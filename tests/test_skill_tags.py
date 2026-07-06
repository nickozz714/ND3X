"""Skill tags (routing_tags) — schema coercion + repository normalization (TODO §1)."""
from __future__ import annotations

import types

from repository.skill_repository import SkillRepository
from schemas.skill import SkillBase, SkillCreate, SkillRead, SkillUpdate


def test_skillbase_coerces_null_tags_to_empty_list():
    # Legacy rows store NULL routing_tags → must read back as [] (not error).
    obj = types.SimpleNamespace(
        name="x", display_name=None, description="", instructions="",
        input_schema=None, output_schema=None, is_system=False, is_runtime=False,
        is_enabled=True, priority=100, source="local", source_name=None,
        version="1.0.0", routing_tags=None, id=1, created_at=None, updated_at=None,
    )
    read = SkillRead.model_validate(obj)
    assert read.routing_tags == []


def test_skillcreate_accepts_tags():
    sc = SkillCreate(name="x", routing_tags=["pm", "azure"])
    assert sc.routing_tags == ["pm", "azure"]


def test_skillupdate_tags_optional_unset_is_none():
    su = SkillUpdate(name="x")
    dumped = su.model_dump(exclude_unset=True)
    assert "routing_tags" not in dumped  # unset → not persisted (partial update)


def test_normalize_tags_trims_dedupes_drops_blanks():
    norm = SkillRepository._normalize_tags(["  pm ", "PM", "", "azure", "azure", "  "])
    assert norm == ["pm", "azure"]  # trimmed, case-insensitive dedupe (first casing), blanks gone


def test_normalize_tags_none_stays_none():
    assert SkillRepository._normalize_tags(None) is None
