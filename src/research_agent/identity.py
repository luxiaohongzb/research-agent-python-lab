from __future__ import annotations

import asyncio
import hmac
import importlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from research_agent.audit import AuditEvent, AuditStore, InMemoryAuditStore
from research_agent.auth import (
    AccessTokenClaims,
    PasswordHasher,
    Permission,
    Principal,
    Role,
    TokenManager,
    permissions_for_roles,
)


class IdentityError(ValueError):
    pass


class InvalidCredentialsError(IdentityError):
    pass


class UserNotFoundError(IdentityError):
    pass


class DuplicateUserError(IdentityError):
    pass


class UnsafeUserChangeError(IdentityError):
    pass


class IdentityModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UserRecord(IdentityModel):
    user_id: str = Field(default_factory=lambda: f"user-{uuid4().hex}")
    tenant_id: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    password_hash: str
    roles: frozenset[Role]
    is_active: bool = True
    token_version: int = Field(default=0, ge=0)
    failed_login_attempts: int = Field(default=0, ge=0)
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UserView(IdentityModel):
    user_id: str
    tenant_id: str
    email: str
    display_name: str
    roles: frozenset[Role]
    permissions: frozenset[Permission]
    is_active: bool
    locked_until: datetime | None
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RefreshSession(IdentityModel):
    session_id: str = Field(default_factory=lambda: f"session-{uuid4().hex}")
    user_id: str
    tenant_id: str
    secret_hash: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None
    replaced_by: str | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=32, max_length=1_024)


class LogoutRequest(RefreshRequest):
    pass


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password(value)


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=256)
    roles: frozenset[Role] = Field(
        default_factory=lambda: frozenset({Role.RESEARCHER}),
        min_length=1,
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return _validate_password(value)


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    roles: frozenset[Role] | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(min_length=12, max_length=256)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password(value)


class TokenPair(IdentityModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: UserView


class RoleView(IdentityModel):
    role: Role
    permissions: frozenset[Permission]


class UserStore(Protocol):
    async def initialize(self) -> None: ...

    async def create(self, user: UserRecord) -> UserRecord: ...

    async def save(self, user: UserRecord) -> None: ...

    async def get(self, user_id: str, *, tenant_id: str | None = None) -> UserRecord: ...

    async def get_by_email(self, tenant_id: str, email: str) -> UserRecord | None: ...

    async def list(self, tenant_id: str, *, limit: int, offset: int) -> tuple[UserRecord, ...]: ...

    async def count_admins(self, tenant_id: str) -> int: ...

    async def create_session(self, session: RefreshSession) -> None: ...

    async def get_session(self, session_id: str) -> RefreshSession | None: ...

    async def revoke_session(self, session_id: str, *, replaced_by: str | None = None) -> None: ...

    async def revoke_user_sessions(self, user_id: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryUserStore:
    def __init__(self) -> None:
        self._users: dict[str, UserRecord] = {}
        self._email_index: dict[tuple[str, str], str] = {}
        self._sessions: dict[str, RefreshSession] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def create(self, user: UserRecord) -> UserRecord:
        async with self._lock:
            key = (user.tenant_id, user.email)
            if key in self._email_index:
                raise DuplicateUserError("a user with this email already exists in the tenant")
            self._users[user.user_id] = user
            self._email_index[key] = user.user_id
            return user

    async def save(self, user: UserRecord) -> None:
        async with self._lock:
            if user.user_id not in self._users:
                raise UserNotFoundError(user.user_id)
            self._users[user.user_id] = user

    async def get(self, user_id: str, *, tenant_id: str | None = None) -> UserRecord:
        async with self._lock:
            user = self._users.get(user_id)
        if user is None or (tenant_id is not None and user.tenant_id != tenant_id):
            raise UserNotFoundError(user_id)
        return user

    async def get_by_email(self, tenant_id: str, email: str) -> UserRecord | None:
        async with self._lock:
            user_id = self._email_index.get((tenant_id, email))
            return self._users.get(user_id) if user_id else None

    async def list(self, tenant_id: str, *, limit: int, offset: int) -> tuple[UserRecord, ...]:
        async with self._lock:
            users = sorted(
                (user for user in self._users.values() if user.tenant_id == tenant_id),
                key=lambda item: (item.created_at, item.user_id),
                reverse=True,
            )
        return tuple(users[offset : offset + limit])

    async def count_admins(self, tenant_id: str) -> int:
        async with self._lock:
            return sum(
                user.tenant_id == tenant_id and user.is_active and Role.ADMIN in user.roles
                for user in self._users.values()
            )

    async def create_session(self, session: RefreshSession) -> None:
        async with self._lock:
            self._sessions[session.session_id] = session

    async def get_session(self, session_id: str) -> RefreshSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def revoke_session(self, session_id: str, *, replaced_by: str | None = None) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.revoked_at is None:
                self._sessions[session_id] = session.model_copy(
                    update={"revoked_at": datetime.now(UTC), "replaced_by": replaced_by}
                )

    async def revoke_user_sessions(self, user_id: str) -> None:
        async with self._lock:
            now = datetime.now(UTC)
            for session_id, session in tuple(self._sessions.items()):
                if session.user_id == user_id and session.revoked_at is None:
                    self._sessions[session_id] = session.model_copy(update={"revoked_at": now})

    async def close(self) -> None:
        return None


class PostgresUserStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._ensure_pool()

    async def create(self, user: UserRecord) -> UserRecord:
        pool = await self._ensure_pool()
        try:
            await pool.execute(
                _INSERT_USER,
                user.user_id,
                user.tenant_id,
                user.email,
                user.display_name,
                user.password_hash,
                [role.value for role in sorted(user.roles, key=lambda item: item.value)],
                user.is_active,
                user.token_version,
                user.failed_login_attempts,
                user.locked_until,
                user.last_login_at,
                user.created_at,
                user.updated_at,
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise DuplicateUserError(
                    "a user with this email already exists in the tenant"
                ) from exc
            raise
        return user

    async def save(self, user: UserRecord) -> None:
        pool = await self._ensure_pool()
        result = await pool.execute(
            _UPDATE_USER,
            user.user_id,
            user.display_name,
            user.password_hash,
            [role.value for role in sorted(user.roles, key=lambda item: item.value)],
            user.is_active,
            user.token_version,
            user.failed_login_attempts,
            user.locked_until,
            user.last_login_at,
            user.updated_at,
        )
        if result.endswith(" 0"):
            raise UserNotFoundError(user.user_id)

    async def get(self, user_id: str, *, tenant_id: str | None = None) -> UserRecord:
        pool = await self._ensure_pool()
        if tenant_id is None:
            row = await pool.fetchrow("SELECT * FROM identity_users WHERE user_id = $1", user_id)
        else:
            row = await pool.fetchrow(
                "SELECT * FROM identity_users WHERE user_id = $1 AND tenant_id = $2",
                user_id,
                tenant_id,
            )
        if row is None:
            raise UserNotFoundError(user_id)
        return _user_from_row(row)

    async def get_by_email(self, tenant_id: str, email: str) -> UserRecord | None:
        pool = await self._ensure_pool()
        row = await pool.fetchrow(
            "SELECT * FROM identity_users WHERE tenant_id = $1 AND email = $2",
            tenant_id,
            email,
        )
        return _user_from_row(row) if row else None

    async def list(self, tenant_id: str, *, limit: int, offset: int) -> tuple[UserRecord, ...]:
        pool = await self._ensure_pool()
        rows = await pool.fetch(
            """
            SELECT * FROM identity_users WHERE tenant_id = $1
            ORDER BY created_at DESC, user_id DESC LIMIT $2 OFFSET $3
            """,
            tenant_id,
            limit,
            offset,
        )
        return tuple(_user_from_row(row) for row in rows)

    async def count_admins(self, tenant_id: str) -> int:
        pool = await self._ensure_pool()
        return int(
            await pool.fetchval(
                """
                SELECT count(*) FROM identity_users
                WHERE tenant_id = $1 AND is_active AND roles @> '["ADMIN"]'::jsonb
                """,
                tenant_id,
            )
        )

    async def create_session(self, session: RefreshSession) -> None:
        pool = await self._ensure_pool()
        await pool.execute(
            """
            INSERT INTO identity_refresh_sessions
                (session_id, user_id, tenant_id, secret_hash, expires_at,
                 created_at, revoked_at, replaced_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            session.session_id,
            session.user_id,
            session.tenant_id,
            session.secret_hash,
            session.expires_at,
            session.created_at,
            session.revoked_at,
            session.replaced_by,
        )

    async def get_session(self, session_id: str) -> RefreshSession | None:
        pool = await self._ensure_pool()
        row = await pool.fetchrow(
            "SELECT * FROM identity_refresh_sessions WHERE session_id = $1", session_id
        )
        return RefreshSession.model_validate(dict(row)) if row else None

    async def revoke_session(self, session_id: str, *, replaced_by: str | None = None) -> None:
        pool = await self._ensure_pool()
        await pool.execute(
            """
            UPDATE identity_refresh_sessions
            SET revoked_at = COALESCE(revoked_at, now()), replaced_by = COALESCE($2, replaced_by)
            WHERE session_id = $1
            """,
            session_id,
            replaced_by,
        )

    async def revoke_user_sessions(self, user_id: str) -> None:
        pool = await self._ensure_pool()
        await pool.execute(
            """
            UPDATE identity_refresh_sessions SET revoked_at = COALESCE(revoked_at, now())
            WHERE user_id = $1
            """,
            user_id,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                try:
                    asyncpg = importlib.import_module("asyncpg")
                except ImportError as exc:
                    raise RuntimeError('User store requires the "postgres" extra') from exc

                async def initialize(connection: Any) -> None:
                    await connection.set_type_codec(
                        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
                    )

                self._pool = await asyncpg.create_pool(self._dsn, init=initialize)
                await self._pool.execute(_SCHEMA)
        return self._pool


class IdentityService:
    def __init__(
        self,
        store: UserStore,
        token_manager: TokenManager,
        *,
        audit: AuditStore | None = None,
        refresh_ttl_seconds: int = 30 * 24 * 60 * 60,
        max_failed_attempts: int = 5,
        lockout_seconds: int = 15 * 60,
    ) -> None:
        self._store = store
        self._tokens = token_manager
        self._audit = audit or InMemoryAuditStore()
        self._passwords = PasswordHasher()
        self._refresh_ttl_seconds = refresh_ttl_seconds
        self._max_failed_attempts = max_failed_attempts
        self._lockout_seconds = lockout_seconds
        self._dummy_password_hash = self._passwords.hash(secrets.token_urlsafe(32))

    async def initialize(
        self,
        *,
        bootstrap_tenant_id: str | None = None,
        bootstrap_email: str | None = None,
        bootstrap_password: str | None = None,
        bootstrap_display_name: str = "System Administrator",
    ) -> None:
        await self._store.initialize()
        supplied = (bootstrap_tenant_id, bootstrap_email, bootstrap_password)
        if any(supplied) and not all(supplied):
            raise RuntimeError("bootstrap tenant, email, and password must be configured together")
        if not all(supplied):
            return
        assert bootstrap_tenant_id is not None
        assert bootstrap_email is not None
        assert bootstrap_password is not None
        email = _normalize_email(bootstrap_email)
        existing = await self._store.get_by_email(bootstrap_tenant_id, email)
        if existing is not None:
            return
        request = CreateUserRequest(
            email=email,
            display_name=bootstrap_display_name,
            password=bootstrap_password,
            roles=frozenset({Role.ADMIN, Role.RESEARCHER, Role.REVIEWER}),
        )
        await self.create_user(
            tenant_id=bootstrap_tenant_id,
            request=request,
            actor="system:bootstrap",
        )

    async def login(self, request: LoginRequest) -> TokenPair:
        user = await self._store.get_by_email(request.tenant_id, request.email)
        encoded = user.password_hash if user is not None else self._dummy_password_hash
        password_valid = self._passwords.verify(request.password, encoded)
        now = datetime.now(UTC)
        locked = user is not None and user.locked_until is not None and user.locked_until > now
        if user is None or not password_valid or not user.is_active or locked:
            if user is not None and user.is_active and not locked:
                attempts = user.failed_login_attempts + 1
                locked_until = (
                    now + timedelta(seconds=self._lockout_seconds)
                    if attempts >= self._max_failed_attempts
                    else None
                )
                await self._store.save(
                    user.model_copy(
                        update={
                            "failed_login_attempts": attempts,
                            "locked_until": locked_until,
                            "updated_at": now,
                        }
                    )
                )
            await self._audit.append(
                AuditEvent(
                    tenant_id=request.tenant_id,
                    actor=request.email,
                    action="identity.login_failed",
                    details={"reason": "invalid_credentials"},
                )
            )
            raise InvalidCredentialsError("invalid email, password, or account state")
        active = user.model_copy(
            update={
                "failed_login_attempts": 0,
                "locked_until": None,
                "last_login_at": now,
                "updated_at": now,
            }
        )
        await self._store.save(active)
        await self._audit.append(
            AuditEvent(
                tenant_id=active.tenant_id,
                actor=active.email,
                action="identity.login_succeeded",
                details={"user_id": active.user_id},
            )
        )
        return await self._issue_pair(active)

    async def refresh(self, refresh_token: str) -> TokenPair:
        session_id, secret = _split_refresh_token(refresh_token)
        session = await self._store.get_session(session_id)
        now = datetime.now(UTC)
        if session is None:
            raise InvalidCredentialsError("invalid refresh token")
        if session.revoked_at is not None:
            await self._store.revoke_user_sessions(session.user_id)
            try:
                compromised = await self._store.get(session.user_id, tenant_id=session.tenant_id)
                await self._store.save(
                    compromised.model_copy(
                        update={
                            "token_version": compromised.token_version + 1,
                            "updated_at": now,
                        }
                    )
                )
            except UserNotFoundError:
                pass
            raise InvalidCredentialsError("refresh token reuse detected")
        valid_secret = hmac.compare_digest(
            session.secret_hash, self._tokens.hash_refresh_secret(secret)
        )
        if not valid_secret or session.expires_at <= now:
            raise InvalidCredentialsError("invalid or expired refresh token")
        user = await self._store.get(session.user_id, tenant_id=session.tenant_id)
        if not user.is_active:
            await self._store.revoke_user_sessions(user.user_id)
            raise InvalidCredentialsError("account is inactive")
        pair, replacement = await self._build_pair(user)
        await self._store.create_session(replacement)
        await self._store.revoke_session(session.session_id, replaced_by=replacement.session_id)
        return pair

    async def logout(self, refresh_token: str, *, actor: str) -> None:
        session_id, secret = _split_refresh_token(refresh_token)
        session = await self._store.get_session(session_id)
        if session is None or not hmac.compare_digest(
            session.secret_hash, self._tokens.hash_refresh_secret(secret)
        ):
            return
        await self._store.revoke_session(session_id)
        await self._audit.append(
            AuditEvent(
                tenant_id=session.tenant_id,
                actor=actor,
                action="identity.logout",
                details={"user_id": session.user_id},
            )
        )

    async def resolve_principal(self, claims: AccessTokenClaims) -> Principal | None:
        try:
            user = await self._store.get(claims.sub, tenant_id=claims.tenant_id)
        except UserNotFoundError:
            return None
        if (
            not user.is_active
            or user.email != claims.email
            or user.token_version != claims.token_version
        ):
            return None
        return Principal(
            user_id=user.user_id,
            subject=user.email,
            tenant_id=user.tenant_id,
            roles=user.roles,
            permissions=permissions_for_roles(user.roles),
            auth_type="access_token",
        )

    async def me(self, principal: Principal) -> UserView:
        if principal.user_id is None:
            return UserView(
                user_id=f"external:{principal.subject}",
                tenant_id=principal.tenant_id,
                email=principal.subject,
                display_name=principal.subject,
                roles=principal.roles,
                permissions=principal.permissions or permissions_for_roles(principal.roles),
                is_active=True,
                locked_until=None,
                last_login_at=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        return _to_view(await self._store.get(principal.user_id, tenant_id=principal.tenant_id))

    async def create_user(
        self, *, tenant_id: str, request: CreateUserRequest, actor: str
    ) -> UserView:
        user = UserRecord(
            tenant_id=tenant_id,
            email=request.email,
            display_name=request.display_name.strip(),
            password_hash=self._passwords.hash(request.password),
            roles=request.roles,
        )
        await self._store.create(user)
        await self._audit.append(
            AuditEvent(
                tenant_id=tenant_id,
                actor=actor,
                action="identity.user_created",
                details={"user_id": user.user_id, "roles": ",".join(sorted(user.roles))},
            )
        )
        return _to_view(user)

    async def list_users(
        self, tenant_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[UserView, ...]:
        return tuple(
            _to_view(user) for user in await self._store.list(tenant_id, limit=limit, offset=offset)
        )

    async def get_user(self, user_id: str, *, tenant_id: str) -> UserView:
        return _to_view(await self._store.get(user_id, tenant_id=tenant_id))

    async def update_user(
        self,
        user_id: str,
        *,
        tenant_id: str,
        request: UpdateUserRequest,
        actor: Principal,
    ) -> UserView:
        user = await self._store.get(user_id, tenant_id=tenant_id)
        roles = request.roles if request.roles is not None else user.roles
        is_active = request.is_active if request.is_active is not None else user.is_active
        if actor.user_id == user.user_id and (not is_active or Role.ADMIN not in roles):
            raise UnsafeUserChangeError("an administrator cannot disable or demote itself")
        removes_last_admin = (
            user.is_active
            and Role.ADMIN in user.roles
            and (not is_active or Role.ADMIN not in roles)
            and await self._store.count_admins(tenant_id) <= 1
        )
        if removes_last_admin:
            raise UnsafeUserChangeError("the tenant must retain at least one active administrator")
        security_changed = roles != user.roles or is_active != user.is_active
        updated = user.model_copy(
            update={
                "display_name": (
                    request.display_name.strip()
                    if request.display_name is not None
                    else user.display_name
                ),
                "roles": roles,
                "is_active": is_active,
                "token_version": user.token_version + int(security_changed),
                "updated_at": datetime.now(UTC),
            }
        )
        await self._store.save(updated)
        if security_changed:
            await self._store.revoke_user_sessions(user.user_id)
        await self._audit.append(
            AuditEvent(
                tenant_id=tenant_id,
                actor=actor.subject,
                action="identity.user_updated",
                details={"user_id": user.user_id, "security_changed": security_changed},
            )
        )
        return _to_view(updated)

    async def change_password(self, principal: Principal, request: ChangePasswordRequest) -> None:
        if principal.user_id is None:
            raise UnsafeUserChangeError("API-key identities do not have a local password")
        user = await self._store.get(principal.user_id, tenant_id=principal.tenant_id)
        if not self._passwords.verify(request.current_password, user.password_hash):
            raise InvalidCredentialsError("current password is incorrect")
        await self._set_password(user, request.new_password, actor=principal.subject)

    async def reset_password(
        self,
        user_id: str,
        *,
        tenant_id: str,
        new_password: str,
        actor: str,
    ) -> None:
        user = await self._store.get(user_id, tenant_id=tenant_id)
        await self._set_password(user, new_password, actor=actor, reset=True)

    async def _set_password(
        self, user: UserRecord, password: str, *, actor: str, reset: bool = False
    ) -> None:
        updated = user.model_copy(
            update={
                "password_hash": self._passwords.hash(password),
                "token_version": user.token_version + 1,
                "failed_login_attempts": 0,
                "locked_until": None,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._store.save(updated)
        await self._store.revoke_user_sessions(user.user_id)
        await self._audit.append(
            AuditEvent(
                tenant_id=user.tenant_id,
                actor=actor,
                action="identity.password_reset" if reset else "identity.password_changed",
                details={"user_id": user.user_id},
            )
        )

    async def _issue_pair(self, user: UserRecord) -> TokenPair:
        pair, session = await self._build_pair(user)
        await self._store.create_session(session)
        return pair

    async def _build_pair(self, user: UserRecord) -> tuple[TokenPair, RefreshSession]:
        secret = secrets.token_urlsafe(48)
        session = RefreshSession(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            secret_hash=self._tokens.hash_refresh_secret(secret),
            expires_at=datetime.now(UTC) + timedelta(seconds=self._refresh_ttl_seconds),
        )
        access = self._tokens.issue_access_token(
            user_id=user.user_id,
            email=user.email,
            tenant_id=user.tenant_id,
            roles=user.roles,
            token_version=user.token_version,
        )
        return (
            TokenPair(
                access_token=access,
                refresh_token=f"{session.session_id}.{secret}",
                expires_in=self._tokens.access_ttl_seconds,
                user=_to_view(user),
            ),
            session,
        )

    async def close(self) -> None:
        await self._store.close()


def role_catalog() -> tuple[RoleView, ...]:
    return tuple(
        RoleView(role=role, permissions=permissions_for_roles(frozenset({role}))) for role in Role
    )


def _to_view(user: UserRecord) -> UserView:
    return UserView(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        roles=user.roles,
        permissions=permissions_for_roles(user.roles),
        is_active=user.is_active,
        locked_until=user.locked_until,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
        raise ValueError("email must be a valid address")
    return normalized


def _validate_password(value: str) -> str:
    if value.isspace():
        raise ValueError("password must not contain only whitespace")
    return value


def _split_refresh_token(value: str) -> tuple[str, str]:
    session_id, separator, secret = value.partition(".")
    if not separator or not session_id.startswith("session-") or len(secret) < 32:
        raise InvalidCredentialsError("invalid refresh token")
    return session_id, secret


def _user_from_row(row: Any) -> UserRecord:
    return UserRecord.model_validate(dict(row))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_users (
    user_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    email text NOT NULL,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    roles jsonb NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    token_version integer NOT NULL DEFAULT 0,
    failed_login_attempts integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    last_login_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (tenant_id, email)
);
CREATE INDEX IF NOT EXISTS identity_users_tenant_created_idx
    ON identity_users(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS identity_refresh_sessions (
    session_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES identity_users(user_id) ON DELETE CASCADE,
    tenant_id text NOT NULL,
    secret_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    revoked_at timestamptz,
    replaced_by text
);
CREATE INDEX IF NOT EXISTS identity_sessions_user_idx
    ON identity_refresh_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS identity_sessions_expiry_idx
    ON identity_refresh_sessions(expires_at) WHERE revoked_at IS NULL;
"""

_INSERT_USER = """
INSERT INTO identity_users
    (user_id, tenant_id, email, display_name, password_hash, roles, is_active,
     token_version, failed_login_attempts, locked_until, last_login_at, created_at, updated_at)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12, $13)
"""

_UPDATE_USER = """
UPDATE identity_users SET
    display_name = $2,
    password_hash = $3,
    roles = $4::jsonb,
    is_active = $5,
    token_version = $6,
    failed_login_attempts = $7,
    locked_until = $8,
    last_login_at = $9,
    updated_at = $10
WHERE user_id = $1
"""
