from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, Protocol

from fastapi import Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Role(StrEnum):
    ADMIN = "ADMIN"
    RESEARCHER = "RESEARCHER"
    REVIEWER = "REVIEWER"


class Permission(StrEnum):
    RESEARCH_CREATE = "research:create"
    RESEARCH_READ = "research:read"
    RESEARCH_CANCEL = "research:cancel"
    RESEARCH_REVIEW = "research:review"
    RESEARCH_EXPORT = "research:export"
    CORPUS_WRITE = "corpus:write"
    METRICS_READ = "metrics:read"
    AUDIT_READ = "audit:read"
    OPERATIONS_MANAGE = "operations:manage"
    INTEGRATIONS_READ = "integrations:read"
    USERS_READ = "users:read"
    USERS_WRITE = "users:write"
    ROLES_READ = "roles:read"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.RESEARCHER: frozenset(
        {
            Permission.RESEARCH_CREATE,
            Permission.RESEARCH_READ,
            Permission.RESEARCH_CANCEL,
            Permission.RESEARCH_EXPORT,
            Permission.CORPUS_WRITE,
        }
    ),
    Role.REVIEWER: frozenset(
        {
            Permission.RESEARCH_READ,
            Permission.RESEARCH_REVIEW,
            Permission.RESEARCH_EXPORT,
        }
    ),
}


def permissions_for_roles(roles: frozenset[Role]) -> frozenset[Permission]:
    return frozenset(permission for role in roles for permission in ROLE_PERMISSIONS[role])


class ApiKeyIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=100)
    roles: frozenset[Role] = frozenset({Role.RESEARCHER})


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[Role]
    user_id: str | None = None
    permissions: frozenset[Permission] = frozenset()
    auth_type: str = "api_key"


class AccessTokenClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sub: str
    email: str
    tenant_id: str
    roles: tuple[Role, ...]
    token_version: int = Field(ge=0)
    token_type: str
    jti: str
    iss: str
    aud: str
    iat: int
    exp: int


class TokenError(ValueError):
    pass


class TokenManager:
    """Minimal HS256 access-token codec; refresh tokens remain opaque and server-side."""

    def __init__(
        self,
        secret: str,
        *,
        issuer: str = "research-agent",
        audience: str = "research-agent-api",
        access_ttl_seconds: int = 900,
    ) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("auth token secret must contain at least 32 bytes")
        self._secret = secret.encode("utf-8")
        self._issuer = issuer
        self._audience = audience
        self.access_ttl_seconds = access_ttl_seconds

    def issue_access_token(
        self,
        *,
        user_id: str,
        email: str,
        tenant_id: str,
        roles: frozenset[Role],
        token_version: int,
    ) -> str:
        now = int(time.time())
        claims = {
            "sub": user_id,
            "email": email,
            "tenant_id": tenant_id,
            "roles": sorted(role.value for role in roles),
            "token_version": token_version,
            "token_type": "access",
            "jti": secrets.token_hex(16),
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "exp": now + self.access_ttl_seconds,
        }
        header = {"alg": "HS256", "typ": "JWT"}
        encoded_header = _b64encode_json(header)
        encoded_claims = _b64encode_json(claims)
        signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
        signature = _b64encode(hmac.digest(self._secret, signing_input, "sha256"))
        return f"{encoded_header}.{encoded_claims}.{signature}"

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        parts = token.split(".")
        if len(parts) != 3:
            raise TokenError("malformed access token")
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        expected = hmac.digest(self._secret, signing_input, "sha256")
        try:
            supplied = _b64decode(parts[2])
        except ValueError as exc:
            raise TokenError("malformed access-token signature") from exc
        if not hmac.compare_digest(expected, supplied):
            raise TokenError("invalid access-token signature")
        try:
            header = json.loads(_b64decode(parts[0]))
            payload = json.loads(_b64decode(parts[1]))
            claims = AccessTokenClaims.model_validate(payload)
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise TokenError("invalid access-token claims") from exc
        if header != {"alg": "HS256", "typ": "JWT"}:
            raise TokenError("unsupported access-token header")
        now = int(time.time())
        if claims.token_type != "access":
            raise TokenError("unexpected token type")
        if claims.iss != self._issuer or claims.aud != self._audience:
            raise TokenError("unexpected token issuer or audience")
        if claims.exp <= now or claims.iat > now + 60:
            raise TokenError("access token is expired or not yet valid")
        return claims

    def hash_refresh_secret(self, secret: str) -> str:
        return hmac.new(self._secret, secret.encode("utf-8"), hashlib.sha256).hexdigest()


class PasswordHasher:
    """Versioned scrypt password hashes using only Python's audited stdlib primitive."""

    _n = 2**14
    _r = 8
    _p = 1
    _maxmem = 64 * 1024 * 1024

    def hash(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=self._n,
            r=self._r,
            p=self._p,
            maxmem=self._maxmem,
            dklen=32,
        )
        return f"scrypt$n={self._n}$r={self._r}$p={self._p}${_b64encode(salt)}${_b64encode(digest)}"

    def verify(self, password: str, encoded: str) -> bool:
        try:
            algorithm, n_value, r_value, p_value, salt_value, digest_value = encoded.split("$")
            if algorithm != "scrypt":
                return False
            n = int(n_value.removeprefix("n="))
            r = int(r_value.removeprefix("r="))
            p = int(p_value.removeprefix("p="))
            if (n, r, p) != (self._n, self._r, self._p):
                return False
            expected = _b64decode(digest_value)
            observed = hashlib.scrypt(
                password.encode("utf-8"),
                salt=_b64decode(salt_value),
                n=n,
                r=r,
                p=p,
                maxmem=self._maxmem,
                dklen=len(expected),
            )
            return hmac.compare_digest(observed, expected)
        except (ValueError, TypeError):
            return False


class PrincipalResolver(Protocol):
    async def resolve_principal(self, claims: AccessTokenClaims) -> Principal | None: ...


class Authenticator:
    def __init__(
        self,
        *,
        mode: str,
        api_keys_json: str,
        token_manager: TokenManager | None = None,
        principal_resolver: PrincipalResolver | None = None,
    ) -> None:
        self._mode = mode
        self._token_manager = token_manager
        self._principal_resolver = principal_resolver
        try:
            raw = json.loads(api_keys_json)
            if not isinstance(raw, dict):
                raise ValueError("API key configuration must be an object")
            self._identities = {
                str(key): ApiKeyIdentity.model_validate(value) for key, value in raw.items()
            }
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise RuntimeError(f"Invalid RESEARCH_AGENT_API_KEYS_JSON: {exc}") from exc

    async def authenticate(
        self,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> Principal:
        if self._mode == "disabled":
            roles = frozenset(Role)
            return Principal(
                subject="local-developer",
                tenant_id="default",
                roles=roles,
                permissions=permissions_for_roles(roles),
                auth_type="disabled",
            )
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            self._unauthorized("A Bearer credential is required")
        if self._mode in {"api_key", "hybrid"}:
            identity = next(
                (
                    value
                    for key, value in self._identities.items()
                    if hmac.compare_digest(key, token)
                ),
                None,
            )
            if identity is not None:
                return Principal(
                    subject=identity.subject,
                    tenant_id=identity.tenant_id,
                    roles=identity.roles,
                    permissions=permissions_for_roles(identity.roles),
                    auth_type="api_key",
                )
        if self._mode in {"rbac", "hybrid"}:
            if self._token_manager is None or self._principal_resolver is None:
                raise RuntimeError("RBAC authentication is not configured")
            try:
                claims = self._token_manager.decode_access_token(token)
                principal = await self._principal_resolver.resolve_principal(claims)
            except TokenError:
                principal = None
            if principal is not None:
                return principal
        self._unauthorized("Invalid or expired credential")

    @staticmethod
    def _unauthorized(detail: str) -> NoReturn:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_role(principal: Principal, *allowed: Role) -> None:
    if not principal.roles.intersection(allowed):
        raise HTTPException(status_code=403, detail="Insufficient role")


def require_permission(principal: Principal, *required: Permission) -> None:
    permissions = principal.permissions or permissions_for_roles(principal.roles)
    if not set(required).issubset(permissions):
        raise HTTPException(status_code=403, detail="Insufficient permission")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("invalid base64url value") from exc


def _b64encode_json(value: dict[str, Any]) -> str:
    return _b64encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))
