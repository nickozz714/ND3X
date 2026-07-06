from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.database import Base
from models import assistant as _assistant_models  # noqa: F401
from models import assistant_skill as _assistant_skill_models  # noqa: F401
from models import mcp_server as _mcp_server_models  # noqa: F401
from models import skill as _skill_models  # noqa: F401
from models import skill_file as _skill_file_models  # noqa: F401
from models import skill_tool as _skill_tool_models  # noqa: F401
from models import tool as _tool_models  # noqa: F401
from services.authz_service import assert_expert_role
from schemas.skill import SkillCreate, SkillMarkdownImport
from services.assistants.prompt_builder import PromptBuilder
from services.assistants.runtime_config import AssistantConfig, SkillConfig, SkillFileConfig
from services.assistants.runtime_config_loader import AssistantRuntimeConfigLoader
from services.assistants.skill_file_service import SkillFileService
from services.assistants.skill_service import SkillService
from services.assistants.orchestration.tool_result_artifacts import ToolResultNormalizer
from services.builtin.tools.file_tools import file_inspect, file_metadata, json_inspect


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    monkeypatch.setattr("component.config.settings.FILES_DIR", str(tmp_path / "files"))
    monkeypatch.setattr("component.config.settings.ASK_JOB_ROOT", str(tmp_path / "ask"))
    engine = create_engine("sqlite:///:memory:", future=True, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    db = SessionLocal()
    try:
        yield db, tmp_path
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def skill(db_session):
    db, _ = db_session
    return SkillService(db).create(SkillCreate(name="fabric_capacity_analysis", instructions="Run collector first."))


def _expert():
    return {"id": 1, "email": "expert@example.com", "roles": ["Expert"]}


def _user():
    return {"id": 2, "email": "user@example.com", "roles": ["User"]}


def test_expert_can_create_skill_file_and_list_metadata_only(db_session, skill):
    db, tmp_path = db_session
    assert_expert_role(_expert())
    item = SkillFileService(db).create_or_update_skill_file(
        skill.id,
        "fabric_collect.py",
        "print('collect')\n",
        {"content_type": "text/x-python", "is_executable": True},
    )
    out = SkillFileService(db).to_metadata(item, include_content=False)
    assert out["relative_path"] == "fabric_collect.py"
    assert out["runtime_path"].endswith(f"/files/skills/{skill.id}/fabric_collect.py")
    assert Path(out["runtime_path"]).read_text() == "print('collect')\n"
    assert "content" not in out

    listed = SkillFileService(db).list_skill_files(skill.id)
    assert len(listed) == 1
    assert listed[0]["checksum_sha256"] == hashlib.sha256(b"print('collect')\n").hexdigest()
    assert "content" not in listed[0]
    assert Path(listed[0]["runtime_path"]).is_file()
    assert Path(listed[0]["runtime_path"]).is_relative_to(tmp_path / "files" / "skills")


def test_non_expert_cannot_create_update_delete_skill_files(db_session, skill):
    db, _ = db_session
    for _action in ("create", "update", "delete"):
        with pytest.raises(HTTPException) as exc:
            assert_expert_role(_user())
        assert exc.value.status_code == 403


def test_path_traversal_and_absolute_paths_rejected(db_session, skill):
    db, _ = db_session
    svc = SkillFileService(db)
    with pytest.raises(HTTPException) as traversal:
        svc.create_or_update_skill_file(skill.id, "../escape.txt", "bad")
    assert traversal.value.status_code == 400
    with pytest.raises(HTTPException) as absolute:
        svc.create_or_update_skill_file(skill.id, "/tmp/escape.txt", "bad")
    assert absolute.value.status_code == 400


def test_update_changes_content_size_and_checksum_then_detail_includes_content(db_session, skill):
    db, _ = db_session
    svc = SkillFileService(db)
    item = svc.create_or_update_skill_file(skill.id, "metrics.json", '{"a":1}', {"content_type": "application/json"})
    first = svc.to_metadata(item)
    updated = svc.update_skill_file(skill.id, item.id, content='{"a":2,"b":3}', metadata={"content_type": "application/json"})
    second = svc.get_skill_file(skill.id, updated.id, include_content=True)
    assert second["content"] == '{"a":2,"b":3}'
    assert second["size_bytes"] != first["size_bytes"]
    assert second["checksum_sha256"] != first["checksum_sha256"]
    assert second["checksum_sha256"] == hashlib.sha256(b'{"a":2,"b":3}').hexdigest()


def test_delete_removes_db_row_and_file(db_session, skill):
    db, _ = db_session
    svc = SkillFileService(db)
    item = svc.create_or_update_skill_file(skill.id, "delete-me.txt", "bye")
    path = Path(svc.to_metadata(item)["runtime_path"])
    assert path.exists()
    svc.delete_skill_file(skill.id, item.id)
    assert not path.exists()
    assert svc.list_skill_files(skill.id) == []



def test_runtime_config_loader_attaches_skill_file_metadata(db_session, skill):
    db, _ = db_session
    item = SkillFileService(db).create_or_update_skill_file(skill.id, "fabric_collect.py", "print(1)", {"content_type": "text/x-python"})
    loader = AssistantRuntimeConfigLoader.__new__(AssistantRuntimeConfigLoader)
    loader.db = db
    cfg = loader._skill_to_config(skill, tools=[])
    assert cfg.skill_files_root.endswith(f"/files/skills/{skill.id}")
    assert cfg.skill_files[0].relative_path == "fabric_collect.py"
    assert cfg.skill_files[0].runtime_path == SkillFileService(db).to_metadata(item)["runtime_path"]


def test_active_skill_manifest_includes_metadata_root_and_no_content():
    assistant = AssistantConfig(
        id=1,
        name="a",
        skills=[
            SkillConfig(
                id=10,
                name="fabric_capacity_analysis",
                instructions="Run collector first.",
                skill_files_root="/app/files/skills/fabric_capacity_analysis",
                skill_files=[
                    SkillFileConfig(
                        relative_path="fabric_collect.py",
                        runtime_path="/app/files/skills/fabric_capacity_analysis/fabric_collect.py",
                        content_type="text/x-python",
                        size_bytes=12,
                        checksum_sha256="abc",
                        is_executable=True,
                    )
                ],
            )
        ],
    )
    manifest = PromptBuilder().render_skill_manifest(assistant, selected_skill_names=["fabric_capacity_analysis"])
    assert '"skill_files_root": "/app/files/skills/fabric_capacity_analysis"' in manifest
    assert '"relative_path": "fabric_collect.py"' in manifest
    assert '"runtime_path": "/app/files/skills/fabric_capacity_analysis/fabric_collect.py"' in manifest
    assert '"content":' not in manifest


def test_generated_skill_outputs_are_readable_and_outside_paths_rejected(db_session, skill, tmp_path):
    db, _ = db_session
    svc = SkillFileService(db)
    root = Path(svc.runtime_root_for(skill.id))
    root.mkdir(parents=True, exist_ok=True)
    generated = root / "fabric_telemetry.json"
    generated.write_text('{"rows": [{"capacity": 42}]}', encoding="utf-8")

    metadata = asyncio.run(file_metadata({"local_path": str(generated)}))
    assert metadata["status"] == "success"
    inspected = asyncio.run(json_inspect({"local_path": str(generated)}))
    assert inspected["file_type"] == "json"
    generic = asyncio.run(file_inspect({"local_path": str(generated)}))
    assert generic["file_type"] == "json"

    secret = tmp_path / "secret.json"
    secret.write_text('{"secret": true}', encoding="utf-8")
    with pytest.raises(ValueError):
        asyncio.run(file_metadata({"local_path": str(secret)}))
    with pytest.raises(ValueError):
        asyncio.run(file_metadata({"local_path": "/etc/passwd"}))


def test_existing_artifact_root_file_tools_still_work(db_session):
    n = ToolResultNormalizer(thread_id="t", run_id="r")
    out = n._write_artifact_bytes(
        tool_call_id="c",
        tool="x",
        data=b'{"ok": true}',
        filename="artifact.json",
        mime_type="application/json",
        truncated_for_llm=False,
        inspection_level="artifact_only",
    )
    inspected = asyncio.run(json_inspect({"content_ref": out["content_ref"]}))
    assert inspected["file_type"] == "json"


def test_import_markdown_supports_optional_files(db_session):
    db, _ = db_session
    imported = SkillService(db).import_markdown(
        SkillMarkdownImport(
            name="imported_bundle",
            markdown="Instructions remain in DB.",
            files=[{"relative_path": "templates/example.txt", "content": "hello", "content_type": "text/plain"}],
        ),
        user=_expert(),
    )
    assert imported.instructions == "Instructions remain in DB."
    files = SkillFileService(db).list_skill_files(imported.id)
    assert files[0]["relative_path"] == "templates/example.txt"
    assert Path(files[0]["runtime_path"]).read_text() == "hello"
