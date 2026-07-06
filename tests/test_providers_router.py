"""Route-resolution regression test for the providers router.

PUT /admin/providers/assignments must resolve to set_assignment, not be
shadowed by PUT /admin/providers/{provider_id} (which caused a 422
'provider_id' int-parse error on "assignments").
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import models.provider as pv
import routers.providers_router as pr
from authentication.dependencies import require_user
from db.database import get_db


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for m in (pv.Provider, pv.ProviderModel, pv.CapabilityAssignment):
        m.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)

    def override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(pr, "assert_expert_role", lambda user: None)
    app = FastAPI()
    app.include_router(pr.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_user] = lambda: {"id": 1, "roles": ["Expert"]}
    return TestClient(app)


def test_assignment_put_not_shadowed_by_provider_id(client):
    p = client.post("/api/admin/providers", json={"name": "A", "provider_type": "anthropic", "api_key": "k"}).json()
    m = client.post("/api/admin/providers/models",
                    json={"provider_id": p["id"], "model_id": "claude-opus-4-8", "capability": "chat"}).json()

    # The route that previously 422'd:
    r = client.put("/api/admin/providers/assignments",
                   json={"slot": "chat.planner", "provider_model_id": m["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slot"] == "chat.planner" and body["provider_model_id"] == m["id"]
    assert body["model_id"] == "claude-opus-4-8"

    # clearing the slot
    r2 = client.put("/api/admin/providers/assignments", json={"slot": "chat.planner", "provider_model_id": None})
    assert r2.status_code == 200 and r2.json()["provider_model_id"] is None


def test_provider_id_routes_still_work(client):
    p = client.post("/api/admin/providers", json={"name": "B", "provider_type": "openai"}).json()
    r = client.put(f"/api/admin/providers/{p['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r2 = client.delete(f"/api/admin/providers/{p['id']}")
    assert r2.status_code == 200


def test_models_list_route_not_shadowed(client):
    # GET /models must not be parsed as /{provider_id}
    r = client.get("/api/admin/providers/models")
    assert r.status_code == 200 and isinstance(r.json(), list)
