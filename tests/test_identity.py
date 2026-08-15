from __future__ import annotations

import httpx
import pytest

from research_agent.api import create_app
from research_agent.audit import InMemoryAuditStore
from research_agent.auth import Authenticator, PasswordHasher, Role, TokenManager
from research_agent.identity import (
    CreateUserRequest,
    IdentityService,
    InMemoryUserStore,
    LoginRequest,
)
from research_agent.workflow import build_default_workflow


def test_scrypt_password_hashes_are_salted_and_verifiable() -> None:
    hasher = PasswordHasher()
    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")

    assert first != second
    assert hasher.verify("correct horse battery staple", first)
    assert not hasher.verify("wrong password", first)
    assert not hasher.verify("anything", "malformed")


@pytest.mark.asyncio
async def test_rbac_user_lifecycle_refresh_rotation_and_tenant_scope() -> None:
    store = InMemoryUserStore()
    audit = InMemoryAuditStore()
    tokens = TokenManager("test-secret-that-is-longer-than-thirty-two-bytes")
    identity = IdentityService(store, tokens, audit=audit)
    await identity.initialize()
    admin = await identity.create_user(
        tenant_id="tenant-a",
        request=CreateUserRequest(
            email="admin@example.com",
            display_name="Tenant Admin",
            password="Admin password 123!",
            roles=frozenset({Role.ADMIN}),
        ),
        actor="system:test",
    )
    other_tenant_user = await identity.create_user(
        tenant_id="tenant-b",
        request=CreateUserRequest(
            email="other@example.com",
            display_name="Other Tenant",
            password="Other tenant password 123!",
            roles=frozenset({Role.ADMIN}),
        ),
        actor="system:test",
    )
    app = create_app(
        build_default_workflow(),
        audit_store=audit,
        user_store=store,
        identity_service=identity,
        authenticator=Authenticator(
            mode="rbac",
            api_keys_json="{}",
            token_manager=tokens,
            principal_resolver=identity,
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/v1/auth/login",
            json={
                "tenant_id": "tenant-a",
                "email": "ADMIN@example.com",
                "password": "Admin password 123!",
            },
        )
        assert login.status_code == 200
        admin_access = login.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_access}"}

        created = await client.post(
            "/v1/users",
            headers=admin_headers,
            json={
                "email": "researcher@example.com",
                "display_name": "Researcher",
                "password": "Researcher password 123!",
                "roles": ["RESEARCHER"],
            },
        )
        assert created.status_code == 201
        researcher_id = created.json()["user_id"]

        empty_roles = await client.patch(
            f"/v1/users/{researcher_id}",
            headers=admin_headers,
            json={"roles": []},
        )
        assert empty_roles.status_code == 422

        researcher_login = await client.post(
            "/v1/auth/login",
            json={
                "tenant_id": "tenant-a",
                "email": "researcher@example.com",
                "password": "Researcher password 123!",
            },
        )
        assert researcher_login.status_code == 200
        researcher_access = researcher_login.json()["access_token"]
        first_refresh = researcher_login.json()["refresh_token"]
        researcher_headers = {"Authorization": f"Bearer {researcher_access}"}

        me = await client.get("/v1/auth/me", headers=researcher_headers)
        forbidden = await client.get("/v1/users", headers=researcher_headers)
        assert me.status_code == 200
        assert set(me.json()["permissions"]) == {
            "corpus:write",
            "research:cancel",
            "research:create",
            "research:export",
            "research:read",
        }
        assert forbidden.status_code == 403
        hidden_other_tenant = await client.get(
            f"/v1/users/{other_tenant_user.user_id}", headers=admin_headers
        )
        assert hidden_other_tenant.status_code == 404

        rotated = await client.post("/v1/auth/refresh", json={"refresh_token": first_refresh})
        assert rotated.status_code == 200
        assert rotated.json()["refresh_token"] != first_refresh

        reuse = await client.post("/v1/auth/refresh", json={"refresh_token": first_refresh})
        assert reuse.status_code == 401
        compromised_access = await client.get("/v1/auth/me", headers=researcher_headers)
        assert compromised_access.status_code == 401

        disabled = await client.patch(
            f"/v1/users/{researcher_id}",
            headers=admin_headers,
            json={"is_active": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["is_active"] is False

        cannot_remove_last_admin = await client.patch(
            f"/v1/users/{admin.user_id}",
            headers=admin_headers,
            json={"roles": ["RESEARCHER"]},
        )
        assert cannot_remove_last_admin.status_code == 409

    events = await audit.list("tenant-a", limit=100)
    assert {event.action for event in events} >= {
        "identity.login_succeeded",
        "identity.user_created",
        "identity.user_updated",
    }


@pytest.mark.asyncio
async def test_bootstrap_admin_is_idempotent() -> None:
    store = InMemoryUserStore()
    identity = IdentityService(
        store,
        TokenManager("bootstrap-test-secret-that-is-longer-than-32-bytes"),
    )

    for _ in range(2):
        await identity.initialize(
            bootstrap_tenant_id="tenant-a",
            bootstrap_email="bootstrap@example.com",
            bootstrap_password="Bootstrap password 123!",
            bootstrap_display_name="Bootstrap Admin",
        )

    users = await identity.list_users("tenant-a")
    assert len(users) == 1
    assert users[0].roles == frozenset({Role.ADMIN, Role.RESEARCHER, Role.REVIEWER})


@pytest.mark.asyncio
async def test_failed_logins_lock_account_until_admin_reset() -> None:
    store = InMemoryUserStore()
    tokens = TokenManager("another-test-secret-that-is-longer-than-32-bytes")
    identity = IdentityService(store, tokens, max_failed_attempts=2, lockout_seconds=600)
    await identity.create_user(
        tenant_id="tenant-a",
        request=CreateUserRequest(
            email="locked@example.com",
            display_name="Locked User",
            password="Correct password 123!",
            roles=frozenset({Role.RESEARCHER}),
        ),
        actor="system:test",
    )

    for _ in range(2):
        with pytest.raises(ValueError, match="invalid email"):
            await identity.login(
                LoginRequest(
                    tenant_id="tenant-a",
                    email="locked@example.com",
                    password="wrong",
                )
            )
    user = await store.get_by_email("tenant-a", "locked@example.com")
    assert user is not None
    assert user.locked_until is not None
