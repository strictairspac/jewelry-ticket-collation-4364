"""End-to-end HTTP tests for the cancel endpoint using a temporary file database."""
import os
import tempfile
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from src.config import resources
from src.constants.job_state import JobState
from src.models.audit import audit
from src.models.base import metadata
from src.models.cases import cases
from src.models.runs import runs
from src.models.users import users
from src.utils.passwords import hash_password


SECRET = "test-secret"


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    patch = MonkeyPatch()
    yield patch
    patch.undo()


@pytest.fixture(scope="module")
def client(monkeypatch_module):
    # A file database gives the TestClient's portal thread and the test thread separate
    # pooled connections with proper transaction isolation, mirroring the MySQL deployment.
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_file.close()
    engine = create_engine(f"sqlite:///{db_file.name}")
    metadata.create_all(engine)

    monkeypatch_module.setattr(resources, "engine", engine)
    # Every module that opens a session captured the engine symbol by value at import time.
    from src.controllers import workbench as workbench_controller
    from src.routes import cases as cases_routes
    from src.routes import runs as runs_routes
    from src.routes import session as session_routes
    from src.routes import system as system_routes
    from src.services import auth as auth_service
    monkeypatch_module.setattr(workbench_controller, "engine", engine)
    monkeypatch_module.setattr(cases_routes, "engine", engine)
    monkeypatch_module.setattr(runs_routes, "engine", engine)
    monkeypatch_module.setattr(system_routes, "engine", engine)
    monkeypatch_module.setattr(auth_service, "engine", engine)
    monkeypatch_module.setattr("src.config.settings.JWT_SECRET", SECRET)
    monkeypatch_module.setattr("src.controllers.access.JWT_SECRET", SECRET)

    with engine.begin() as db:
        for username, role in (("operator", "operator"), ("reviewer", "reviewer"), ("viewer", "viewer")):
            db.execute(users.insert().values(
                username=username, role=role, password_hash=hash_password("pw-12345678")))

    from src.main import app
    with TestClient(app) as test_client:
        test_client.engine = engine
        yield test_client
    engine.dispose()
    os.unlink(db_file.name)


@pytest.fixture()
def seed_runs(client):
    """Create a fresh queued and done batch per test, keeping tests independent."""
    engine = client.engine
    with engine.begin() as db:
        case_id = db.execute(cases.insert().values(
            code=f"CASE-{uuid4().hex[:12].upper()}",
            title="冬展卷", description="d", payload={"tickets": []}, created_by="seed"
        )).inserted_primary_key[0]
        run_id = db.execute(runs.insert().values(
            case_id=case_id, status=JobState.QUEUED, parameter=6, actor="operator", created_at=1
        )).inserted_primary_key[0]
        done_id = db.execute(runs.insert().values(
            case_id=case_id, status=JobState.DONE, parameter=6, actor="operator",
            created_at=1, finished_at=2, result={"summary": "ok"}, digest="abcd"
        )).inserted_primary_key[0]
    yield run_id, done_id
    with engine.begin() as db:
        db.execute(audit.delete())
        db.execute(runs.delete())
        db.execute(cases.delete())


def token_for(username, role):
    return jwt.encode({"sub": username, "role": role, "exp": 9_999_999_999}, SECRET, algorithm="HS256")


def auth(username, role):
    return {"Authorization": f"Bearer {token_for(username, role)}"}


def test_anonymous_cancel_is_rejected(client, seed_runs):
    run_id, _ = seed_runs
    response = client.post(f"/runs/{run_id}/cancel", json={})
    assert response.status_code == 401


def test_reviewer_and_viewer_cannot_cancel(client, seed_runs):
    run_id, _ = seed_runs
    for role in ("reviewer", "viewer"):
        response = client.post(f"/runs/{run_id}/cancel", json={}, headers=auth(role, role))
        assert response.status_code == 403


def test_operator_cancels_queued_batch_and_retry_is_idempotent(client, seed_runs):
    run_id, _ = seed_runs
    response = client.post(f"/runs/{run_id}/cancel", json={"reason": "选错卷"}, headers=auth("operator", "operator"))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cancel_reason"] == "选错卷"
    assert body["cancelled_by"] == "operator"

    # Lost receipt -> safe retry, no second audit row.
    retry = client.post(f"/runs/{run_id}/cancel", json={"reason": "选错卷"}, headers=auth("operator", "operator"))
    assert retry.status_code == 200
    assert retry.json()["status"] == "cancelled"

    engine = client.engine
    with engine.connect() as db:
        cancel_events = db.execute(select(audit).where(
            audit.c.entity == "run", audit.c.entity_id == run_id, audit.c.action == "cancel")).all()
    assert len(cancel_events) == 1


def test_cancel_without_body_is_accepted(client, seed_runs):
    run_id, _ = seed_runs
    response = client.post(f"/runs/{run_id}/cancel", headers=auth("operator", "operator"))
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancel_reason"] is None


def test_cancel_done_batch_conflicts_and_export_still_works(client, seed_runs):
    _, done_id = seed_runs
    conflict = client.post(f"/runs/{done_id}/cancel", json={}, headers=auth("operator", "operator"))
    assert conflict.status_code == 409

    export = client.get(f"/runs/{done_id}/export", headers=auth("operator", "operator"))
    assert export.status_code == 200
    assert export.headers["content-type"] == "application/json"


def test_cancelled_batch_has_no_export_and_cannot_be_reviewed(client, seed_runs):
    run_id, _ = seed_runs
    client.post(f"/runs/{run_id}/cancel", json={}, headers=auth("operator", "operator"))

    export = client.get(f"/runs/{run_id}/export", headers=auth("reviewer", "reviewer"))
    assert export.status_code == 409

    review = client.post(
        f"/runs/{run_id}/review", json={"note": "不应能签署"}, headers=auth("reviewer", "reviewer"))
    assert review.status_code == 409


def test_cancel_unknown_run_returns_404(client, seed_runs):
    response = client.post("/runs/9999/cancel", json={}, headers=auth("operator", "operator"))
    assert response.status_code == 404


def test_run_listing_exposes_cancellation_fields(client, seed_runs):
    run_id, _ = seed_runs
    response = client.get("/runs", headers=auth("viewer", "viewer"))
    assert response.status_code == 200
    entry = next(item for item in response.json() if item["id"] == run_id)
    assert "cancel_reason" in entry and "cancelled_by" in entry
