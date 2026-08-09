from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Role(StrEnum):
    ADMIN = "ADMIN"
    RESEARCHER = "RESEARCHER"
    REVIEWER = "REVIEWER"


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


class Authenticator:
    def __init__(self, *, mode: str, api_keys_json: str) -> None:
        self._mode = mode
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
            return Principal(
                subject="local-developer",
                tenant_id="default",
                roles=frozenset(Role),
            )
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A Bearer API key is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        identity = next(
            (value for key, value in self._identities.items() if hmac.compare_digest(key, token)),
            None,
        )
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return Principal(
            subject=identity.subject,
            tenant_id=identity.tenant_id,
            roles=identity.roles,
        )


def require_role(principal: Principal, *allowed: Role) -> None:
    if not principal.roles.intersection(allowed):
        raise HTTPException(status_code=403, detail="Insufficient role")
