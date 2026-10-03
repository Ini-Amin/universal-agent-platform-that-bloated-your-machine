"""Tests for multi-user identity, roles, key management, and shared workspaces.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from uap.db import Base, create_db_engine, create_session_factory, get_database_url
from uap.db.repositories import UserRepository, WorkspaceMemberRepository, WorkspaceRepository
from uap.db.models.user import UserRow, WorkspaceMemberRow
from uap.db.models.workspace import WorkspaceRow
from uap.server.app import create_app
from uap.users import Role, generate_api_key, hash_api_key

RESEARCH_INPUT = "research distributed consensus algorithms"
TEST_DATABASE_URL = get_database_url()
BOOTSTRAP_TOKEN = "bootstrap_admin_secret_token_12345"


@pytest.fixture(scope="module")
def engine(isolated_engine: Engine) -> Engine:
    return isolated_engine

@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: Engine) -> FastAPI:
    monkeypatch.setattr("uap.db.engine.get_engine", lambda: engine)
    monkeypatch.setattr("uap.db.engine.get_session_factory", lambda: create_session_factory(engine))
    return create_app(runs_dir=tmp_path / "runs", run_inline=True, heartbeat_interval=0.05)


def test_key_generation_and_hashing() -> None:
    """Keys are high-entropy, prefixed with uap_, and hashed with SHA-256."""
    raw_key = generate_api_key()
    assert raw_key.startswith("uap_")
    assert len(raw_key) > 30

    h1 = hash_api_key(raw_key)
    h2 = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    assert h1 == h2
    assert len(h1) == 64
    assert hash_api_key(raw_key) == h1


def test_user_repository_crud_and_lookup(session_factory: sessionmaker[Session]) -> None:
    """UserRepository can create, lookup by hash, rotate key, and deactivate."""
    with session_factory() as session:
        repo = UserRepository(session)
        raw_key = generate_api_key()
        key_hash = hash_api_key(raw_key)
        alice_id = f"user-alice-{uuid.uuid4().hex[:6]}"
        user = repo.create(
            name="Alice Admin",
            role=Role.ADMIN,
            email="alice@example.com",
            api_key_hash=key_hash,
            user_id=alice_id,
        )
        session.commit()

        assert user.id == alice_id
        assert user.role == Role.ADMIN
        assert user.is_active is True

    # Lookup by api_key_hash
    with session_factory() as session:
        repo = UserRepository(session)
        found = repo.get_by_api_key_hash(key_hash)
        assert found is not None
        assert found.id == alice_id

        # Rotate key
        new_key = generate_api_key()
        new_hash = hash_api_key(new_key)
        rotated = repo.rotate_key(alice_id, new_hash)
        assert rotated.api_key_hash == new_hash
        session.commit()

    # Old key hash no longer matches
    with session_factory() as session:
        repo = UserRepository(session)
        assert repo.get_by_api_key_hash(key_hash) is None
        assert repo.get_by_api_key_hash(new_hash) is not None

        # Deactivate
        repo.deactivate(alice_id)
        session.commit()

    with session_factory() as session:
        repo = UserRepository(session)
        deactivated = repo.get(alice_id)
        assert deactivated.is_active is False


def test_workspace_members_repo(session_factory: sessionmaker[Session]) -> None:
    """WorkspaceMemberRepository manages workspace membership."""
    with session_factory() as session:
        ws_repo = WorkspaceRepository(session)
        user_repo = UserRepository(session)
        member_repo = WorkspaceMemberRepository(session)

        bob_key = f"bob_key_{uuid.uuid4().hex[:6]}"
        u = user_repo.create(name="Bob", role=Role.MEMBER, api_key_hash=hash_api_key(bob_key))
        ws_id = f"ws-{uuid.uuid4().hex[:6]}"
        ws = ws_repo.create(
            type("WS", (), {"id": ws_id, "name": "Team Space", "owner_id": u.id})()
        )
        session.flush()

        member_repo.add_member(ws.id, u.id, role="owner")
        session.commit()

        members = member_repo.list_members(ws.id)
        assert len(members) == 1
        assert members[0].user_id == u.id
        assert members[0].role == "owner"

        user_workspaces = member_repo.list_user_workspaces(u.id)
        assert ws.id in user_workspaces


def test_admin_creates_and_lists_users(
    app: FastAPI, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap admin token can create users, get raw key once, and list users without keys."""
    monkeypatch.setenv("UAP_API_TOKEN", BOOTSTRAP_TOKEN)
    client = TestClient(app)
    admin_auth = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}

    # Create member
    resp = client.post(
        "/api/users",
        json={"name": "Carol Member", "email": "carol@example.com", "role": "member"},
        headers=admin_auth,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Carol Member"
    assert data["role"] == "member"
    assert "api_key" in data
    carol_key = data["api_key"]
    assert carol_key.startswith("uap_")
    carol_id = data["id"]

    # List users
    list_resp = client.get("/api/users", headers=admin_auth)
    assert list_resp.status_code == 200
    users = list_resp.json()
    assert any(u["id"] == carol_id for u in users)
    # Crucial: raw api_key or hash MUST NEVER be in list
    for u in users:
        assert "api_key" not in u
        assert "api_key_hash" not in u


def test_role_enforcement(
    app: FastAPI, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roles enforced: viewer cannot POST /tasks (403), member cannot create users (403)."""
    monkeypatch.setenv("UAP_API_TOKEN", BOOTSTRAP_TOKEN)
    client = TestClient(app)
    admin_auth = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}

    # Create a member and a viewer
    m_resp = client.post(
        "/api/users",
        json={"name": "Dan Member", "role": "member"},
        headers=admin_auth,
    )
    assert m_resp.status_code == 200
    member_key = m_resp.json()["api_key"]

    v_resp = client.post(
        "/api/users",
        json={"name": "Eve Viewer", "role": "viewer"},
        headers=admin_auth,
    )
    assert v_resp.status_code == 200
    viewer_key = v_resp.json()["api_key"]

    member_auth = {"Authorization": f"Bearer {member_key}"}
    viewer_auth = {"Authorization": f"Bearer {viewer_key}"}

    # 1. Member cannot create user -> 403
    forbidden_user = client.post(
        "/api/users",
        json={"name": "Mallory", "role": "member"},
        headers=member_auth,
    )
    assert forbidden_user.status_code == 403

    # 2. Viewer cannot create user -> 403
    forbidden_viewer_user = client.post(
        "/api/users",
        json={"name": "Mallory", "role": "member"},
        headers=viewer_auth,
    )
    assert forbidden_viewer_user.status_code == 403

    # 3. Viewer cannot start task -> 403
    viewer_task = client.post(
        "/tasks",
        json={"input": RESEARCH_INPUT},
        headers=viewer_auth,
    )
    assert viewer_task.status_code == 403

    # 4. Member CAN start task -> 200
    member_task = client.post(
        "/tasks",
        json={"input": RESEARCH_INPUT},
        headers=member_auth,
    )
    assert member_task.status_code == 200

    # 5. Viewer CAN read tasks -> 200
    viewer_read = client.get("/tasks", headers=viewer_auth)
    assert viewer_read.status_code == 200


def test_key_rotation_and_deactivation(
    app: FastAPI, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key rotation immediately invalidates old key; deactivation invalidates active key."""
    monkeypatch.setenv("UAP_API_TOKEN", BOOTSTRAP_TOKEN)
    client = TestClient(app)
    admin_auth = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}

    user_resp = client.post(
        "/api/users",
        json={"name": "Frank Rotate", "role": "member"},
        headers=admin_auth,
    )
    uid = user_resp.json()["id"]
    old_key = user_resp.json()["api_key"]

    # Old key works
    assert client.get("/tasks", headers={"Authorization": f"Bearer {old_key}"}).status_code == 200

    # Rotate key
    rot_resp = client.post(f"/api/users/{uid}/rotate", headers=admin_auth)
    assert rot_resp.status_code == 200
    new_key = rot_resp.json()["api_key"]
    assert new_key != old_key

    # Old key immediately stops working -> 401
    assert client.get("/tasks", headers={"Authorization": f"Bearer {old_key}"}).status_code == 401
    # New key works -> 200
    assert client.get("/tasks", headers={"Authorization": f"Bearer {new_key}"}).status_code == 200

    # Deactivate user
    deact_resp = client.post(f"/api/users/{uid}/deactivate", headers=admin_auth)
    assert deact_resp.status_code == 200
    assert deact_resp.json()["is_active"] is False

    # New key immediately stops working -> 401
    assert client.get("/tasks", headers={"Authorization": f"Bearer {new_key}"}).status_code == 401


def test_identity_resolution_and_forged_user_id(
    app: FastAPI, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """User key sets requested_by from TOKEN; forged body user_id is IGNORED."""
    monkeypatch.setenv("UAP_API_TOKEN", BOOTSTRAP_TOKEN)
    client = TestClient(app)
    admin_auth = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}

    u_resp = client.post(
        "/api/users",
        json={"name": "Grace Hacker", "role": "member"},
        headers=admin_auth,
    )
    grace_id = u_resp.json()["id"]
    grace_key = u_resp.json()["api_key"]

    # Grace tries to forge user_id as 'victim_admin'
    forged_body = {"input": RESEARCH_INPUT, "user_id": "victim_admin"}
    task_resp = client.post(
        "/tasks",
        json=forged_body,
        headers={"Authorization": f"Bearer {grace_key}"},
    )
    assert task_resp.status_code == 200
    task_id = task_resp.json()["task_id"]

    # Check detail / list
    detail = client.get(f"/tasks/{task_id}", headers={"Authorization": f"Bearer {grace_key}"}).json()
    assert detail["requested_by"] == grace_id
    assert detail["requested_by"] != "victim_admin"

    # Filter by Grace finds it
    grace_tasks = client.get(f"/tasks?requested_by={grace_id}", headers={"Authorization": f"Bearer {grace_key}"}).json()
    assert any(t["task_id"] == task_id for t in grace_tasks)

    # Filter by victim_admin does NOT find it
    victim_tasks = client.get("/tasks?requested_by=victim_admin", headers={"Authorization": f"Bearer {grace_key}"}).json()
    assert not any(t["task_id"] == task_id for t in victim_tasks)


def test_shared_workspaces_scoping(
    app: FastAPI, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/workspaces returns only caller's workspaces; admins see all; POST members adds user."""
    monkeypatch.setenv("UAP_API_TOKEN", BOOTSTRAP_TOKEN)
    client = TestClient(app)
    admin_auth = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}

    u1 = client.post("/api/users", json={"name": "User One", "role": "member"}, headers=admin_auth).json()
    u2 = client.post("/api/users", json={"name": "User Two", "role": "member"}, headers=admin_auth).json()
    auth_u1 = {"Authorization": f"Bearer {u1['api_key']}"}
    auth_u2 = {"Authorization": f"Bearer {u2['api_key']}"}

    # User 1 creates a workspace
    ws_resp = client.post("/api/workspaces", json={"name": "Project Apollo"}, headers=auth_u1)
    assert ws_resp.status_code == 200
    apollo_id = ws_resp.json()["id"]

    # User 1 sees Apollo
    u1_spaces = client.get("/api/workspaces", headers=auth_u1).json()
    assert any(ws["id"] == apollo_id for ws in u1_spaces)

    # User 2 does NOT see Apollo
    u2_spaces = client.get("/api/workspaces", headers=auth_u2).json()
    assert not any(ws["id"] == apollo_id for ws in u2_spaces)

    # Admin sees Apollo
    admin_spaces = client.get("/api/workspaces", headers=admin_auth).json()
    assert any(ws["id"] == apollo_id for ws in admin_spaces)

    # Add User 2 to Apollo workspace
    add_mem = client.post(
        f"/api/workspaces/{apollo_id}/members",
        json={"user_id": u2["id"], "role": "member"},
        headers=auth_u1,
    )
    assert add_mem.status_code == 200, add_mem.text

    # Now User 2 sees Apollo
    u2_spaces_after = client.get("/api/workspaces", headers=auth_u2).json()
    assert any(ws["id"] == apollo_id for ws in u2_spaces_after)


def test_backward_compat_no_token_no_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: Engine, apply_migrations
) -> None:
    """When UAP_API_TOKEN is unset and no users exist in DB, platform behaves without auth."""
    # Dedicated scratch schema with zero users
    from sqlalchemy.engine import make_url

    schema = f"user_compat_{uuid.uuid4().hex[:8]}"
    admin = create_db_engine(TEST_DATABASE_URL)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    scoped_url = make_url(TEST_DATABASE_URL).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    eng = create_db_engine(scoped_url.render_as_string(hide_password=False))
    apply_migrations(scoped_url.render_as_string(hide_password=False))
    try:
        monkeypatch.delenv("UAP_API_TOKEN", raising=False)
        monkeypatch.setattr("uap.db.engine.get_engine", lambda: eng)
        monkeypatch.setattr("uap.db.engine.get_session_factory", lambda: create_session_factory(eng))
        compat_app = create_app(runs_dir=tmp_path / "compat_runs", run_inline=True)
        client = TestClient(compat_app)

        # GET /tasks succeeds without auth
        assert client.get("/tasks").status_code == 200

        # POST /tasks succeeds without auth and preserves body.user_id as requested_by
        res = client.post("/tasks", json={"input": RESEARCH_INPUT, "user_id": "legacy_dev"})
        assert res.status_code == 200
        tid = res.json()["task_id"]

        detail = client.get(f"/tasks/{tid}").json()
        assert detail["requested_by"] == "legacy_dev"

        # GET /api/workspaces succeeds without auth
        ws_res = client.get("/api/workspaces")
        assert ws_res.status_code == 200
    finally:
        eng.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
