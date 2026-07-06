from fastapi import HTTPException

from services.authz_service import user_has_role, assert_expert_role
from services.assistants.orchestration.runtime_skill_injection import should_attach_file_artifact_runtime_skill


def test_user_has_role_expert():
    assert user_has_role({"roles": ["Expert"]}, "Expert") is True
    assert user_has_role({"roles": ["admin"]}, "Expert") is True


def test_assert_expert_role_403():
    try:
        assert_expert_role({"roles": ["user"]})
        assert False
    except HTTPException as e:
        assert e.status_code == 403
        assert e.detail == "Expert role required for this action"


def test_runtime_artifact_detector_markers():
    assert should_attach_file_artifact_runtime_skill(question="", payload={"content_ref": "artifact://a/b/c/d"})
