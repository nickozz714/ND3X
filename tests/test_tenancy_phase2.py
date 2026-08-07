"""Multi-tenancy phase 2: token org claim, OrgContext resolution (no-lockout
fallbacks), and org filtering on thread listings."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models.authenticate import User
from models.tenancy import Organization, OrgMembership
from services.auth_service import decode_access_token, make_access_token
from services.tenancy_service import default_org_id, resolve_membership


@pytest.fixture()
def db():
    from sqlalchemy.pool import StaticPool
    # One shared in-memory connection: the repository opens its own sessions
    # (SessionLocal monkeypatch) and must see the same database, also from threads.
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    # Import every model so create_all sees the full schema.
    for m in ("assistant", "assistant_thread", "skill", "tool", "mcp_server", "workflow",
              "provider", "board", "secret", "text_document", "token_usage",
              "meeting_profile", "system_cognition", "audit", "notification_recipient",
              "mail_settings", "fabric_data_agent", "repository", "slash_command",
              "prompt_variable", "application_settings", "tenancy", "authenticate",
              "assistant_tool", "skill_tool", "assistant_skill", "skill_file",
              "transfer", "background_task", "assistant_output_chunk", "log_entry"):
        __import__(f"models.{m}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _user(db, email="a@b.c", roles=None):
    u = User(email=email, password_hash="x", roles=roles or ["User"])
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_token_carries_org_claim():
    tok = make_access_token(1, "a@b.c", ["User"], org_id=7)
    assert decode_access_token(tok)["org"] == 7


def test_pre_tenancy_token_has_no_org_claim():
    tok = make_access_token(1, "a@b.c", ["User"])
    assert "org" not in decode_access_token(tok)


def test_membership_fallback_to_default(db):
    org = Organization(name="X", slug="x")
    db.add(org); db.commit()
    u = _user(db)
    db.add(OrgMembership(org_id=org.id, user_id=u.id, role="owner")); db.commit()
    m = resolve_membership(db, u.id)  # no org requested → default membership
    assert m is not None and m.org_id == org.id and m.role == "owner"
    assert default_org_id(db, u.id) == org.id


def test_membership_self_heal_single_org(db):
    """No-lockout: a membership-less user is healed into the only existing org."""
    org = Organization(name="X", slug="x")
    db.add(org); db.commit()
    u = _user(db)
    m = resolve_membership(db, u.id)
    assert m is not None and m.org_id == org.id and m.role == "member"


def test_membership_not_healed_across_org_boundary(db):
    """With multiple orgs a membership-less user is NOT silently admitted, and a
    claim for an org you don't belong to resolves to None (403 upstream)."""
    a = Organization(name="A", slug="a"); b = Organization(name="B", slug="b")
    db.add_all([a, b]); db.commit()
    u = _user(db)
    assert resolve_membership(db, u.id) is None
    db.add(OrgMembership(org_id=a.id, user_id=u.id, role="member")); db.commit()
    assert resolve_membership(db, u.id, org_id=b.id) is None
    assert resolve_membership(db, u.id, org_id=a.id).org_id == a.id


def test_bootstrap_backfill_then_context(db):
    """The phase-1 backfill + phase-2 resolution end to end: existing users keep
    working (admin becomes owner of the default org)."""
    from db.bootstrap import ensure_default_organization
    admin = _user(db, "admin@b.c", ["Admin"])
    user = _user(db, "u@b.c", ["User"])
    asyncio.run(ensure_default_organization(db))
    assert resolve_membership(db, admin.id).role == "owner"
    assert resolve_membership(db, user.id).role == "member"


def test_thread_list_filters_by_org(db, monkeypatch):
    """Threads from another org are invisible; NULL-org (legacy in-flight) rows
    stay visible during migration."""
    from models.assistant_thread import AssistantThreadModel
    import repository.assistant_thread_repository as repo_mod

    org_a = Organization(name="A", slug="a"); org_b = Organization(name="B", slug="b")
    db.add_all([org_a, org_b]); db.commit()
    db.add_all([
        AssistantThreadModel(id="t-a", title="A", org_id=org_a.id, created_at="2026-01-01", updated_at="2026-01-01"),
        AssistantThreadModel(id="t-b", title="B", org_id=org_b.id, created_at="2026-01-01", updated_at="2026-01-01"),
        AssistantThreadModel(id="t-legacy", title="L", org_id=None, created_at="2026-01-01", updated_at="2026-01-01"),
    ])
    db.commit()

    # Point the repository at this test database.
    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(repo_mod, "SessionLocal", factory)

    repo = repo_mod.AssistantThreadRepository()
    res = asyncio.run(repo.list_threads(org_id=org_a.id, include_archived=True))
    ids = {t["id"] for t in res["items"]}
    assert "t-a" in ids and "t-legacy" in ids and "t-b" not in ids


def test_config_domain_lists_filter_by_org(db):
    """Phase 3: skills/tools/mcp-server listings are org-scoped (NULL = shared)."""
    from models.skill import Skill
    from models.tool import Tool
    from models.mcp_server import MCPServer
    from repository.skill_repository import SkillRepository
    from repository.tool_repository import ToolRepository
    from repository.mcp_server_repository import MCPServerRepository
    from datetime import datetime as _dt
    def _now():
        return _dt.utcnow()

    a = Organization(name="A", slug="a"); b = Organization(name="B", slug="b")
    db.add_all([a, b]); db.commit()
    srv_a = MCPServer(name="m-a", slug="m-a", org_id=a.id, created_at=_now(), updated_at=_now())
    srv_b = MCPServer(name="m-b", slug="m-b", org_id=b.id, created_at=_now(), updated_at=_now())
    db.add_all([srv_a, srv_b]); db.commit()
    db.add_all([
        Skill(name="s-a", org_id=a.id), Skill(name="s-b", org_id=b.id), Skill(name="s-shared"),
        Tool(name="t-a", remote_name="t-a", description="", argument={}, type="mcp",
             tool_instructions="", org_id=a.id, mcp_server_id=srv_a.id,
             created_at=_now(), updated_at=_now()),
        Tool(name="t-b", remote_name="t-b", description="", argument={}, type="mcp",
             tool_instructions="", org_id=b.id, mcp_server_id=srv_b.id,
             created_at=_now(), updated_at=_now()),
    ])
    db.commit()

    skills = {s.name for s in SkillRepository(db).get_all(org_id=a.id)}
    assert skills == {"s-a", "s-shared"}
    tools = {t.name for t in ToolRepository(db).get_all(org_id=a.id)}
    assert tools == {"t-a"}
    servers = {m.name for m in MCPServerRepository(db).get_all(org_id=a.id)}
    assert servers == {"m-a"}


def test_assistants_workflows_providers_filter_by_org(db):
    """Phase 3 part 2: assistants/workflows/providers listings are org-scoped."""
    from datetime import datetime as _dt
    from models.assistant import Assistant
    from models.workflow import Workflow
    from models.provider import Provider
    from repository.assistant_repository import AssistantRepository
    from repository.workflow_repository import WorkflowRepository

    a = Organization(name="A", slug="a"); b = Organization(name="B", slug="b")
    db.add_all([a, b]); db.commit()
    now = _dt.utcnow()
    db.add_all([
        Assistant(name="as-a", description="", instruction="", schema={}, assistant_type="answer", is_active=True, org_id=a.id,
                  created_at=now, updated_at=now),
        Assistant(name="as-b", description="", instruction="", schema={}, assistant_type="answer", is_active=True, org_id=b.id,
                  created_at=now, updated_at=now),
        Workflow(name="wf-a", org_id=a.id, created_at=now, updated_at=now),
        Workflow(name="wf-b", org_id=b.id, created_at=now, updated_at=now),
        Provider(name="p-a", provider_type="openai", org_id=a.id, created_at=now, updated_at=now),
        Provider(name="p-b", provider_type="openai", org_id=b.id, created_at=now, updated_at=now),
    ])
    db.commit()

    assert {x.name for x in AssistantRepository(db).get_all(org_id=a.id)} == {"as-a"}
    assert {x.name for x in WorkflowRepository(db).get_all(org_id=a.id)} == {"wf-a"}
    from services.providers.registry_service import ProviderRegistryService
    provs = {p.name for p in ProviderRegistryService(db).list_providers(org_id=a.id)}
    assert provs == {"p-a"}


def test_project_skill_grants(db):
    """Phase 4: a project with grants limits sessions to those skills; a project
    without grants imposes no restriction (default-open)."""
    from models.assistant_thread import AssistantProjectModel
    from models.skill import Skill
    from models.tenancy import ProjectSkill
    from services.tenancy_service import project_skill_grants

    a = Organization(name="A", slug="a")
    db.add(a); db.commit()
    proj = AssistantProjectModel(id="p1", name="P1", org_id=a.id,
                                 created_at="2026-01-01", updated_at="2026-01-01")
    s1 = Skill(name="granted_skill", org_id=a.id)
    s2 = Skill(name="other_skill", org_id=a.id)
    db.add_all([proj, s1, s2]); db.commit()

    assert project_skill_grants(db, "p1") is None          # no grants → open
    assert project_skill_grants(db, None) is None
    db.add(ProjectSkill(project_id="p1", skill_id=s1.id)); db.commit()
    assert project_skill_grants(db, "p1") == ["granted_skill"]
