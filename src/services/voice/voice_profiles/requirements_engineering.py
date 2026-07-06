# services/voice_profiles/requirements_engineering.py
from __future__ import annotations
from typing import Any, Dict, Optional

from assistants.voice.voice_live_requirements_assistant import VoiceLiveRequirementsAssistant
from assistants.voice.voice_live_requirements_finalize_assistant import VoiceLiveRequirementsFinalizeAssistant

from services.voice.voice_profiles.base import VoiceLiveProfile
from services.markdown.requirements_renderer import requirements_state_to_markdown

class _RequirementsEngineeringProfile(VoiceLiveProfile):
    id = "requirements_engineering"
    name = "Requirements Engineering"

    def empty_state(self) -> Dict[str, Any]:
        return {
            "context": {"title": "Requirements Session", "goal": "", "scope_in": [], "scope_out": []},
            "stakeholders": [],
            "glossary": [],
            "user_stories": [],
            "functional_requirements": [],
            "nonfunctional_requirements": [],
            "assumptions": [],
            "constraints": [],
            "risks": [],
            "decisions": [],
            "open_questions": [],
            "notes": [],
        }

    def assistant(self) -> VoiceLiveRequirementsAssistant:
        return VoiceLiveRequirementsAssistant()

    def render(self, state: Dict[str, Any], *, transcript: Optional[str] = None) -> str:
        return requirements_state_to_markdown(state, transcript=transcript)

    def finalizer(self):
        # kan ook een RE-specifieke finalizer worden later
        return VoiceLiveRequirementsFinalizeAssistant()

requirements_engineering_profile = _RequirementsEngineeringProfile(
    id=_RequirementsEngineeringProfile.id,
    name=_RequirementsEngineeringProfile.name,
)
