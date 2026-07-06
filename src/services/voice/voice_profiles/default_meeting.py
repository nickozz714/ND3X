# services/voice_profiles/default_meeting.py
from __future__ import annotations

from typing import Any, Dict, Optional

from assistants.voice.voice_live_assistant import VoiceLiveAssistant
from assistants.voice.voice_live_cleanup_assistant import VoiceLiveCleanupAssistant

from services.voice.voice_profiles.base import VoiceLiveProfile
from services.markdown.renderer import default_markdown_service  # default meeting renderer


class _DefaultMeetingProfile(VoiceLiveProfile):
    """
    Default profile for general meetings / conversations.
    Acts as the fallback profile.
    """

    id = "default_meeting"
    name = "Default Meeting"

    def empty_state(self) -> Dict[str, Any]:
        """
        Initial accumulated state for live meetings.
        MUST match VoiceLiveAssistant schema.
        """
        return {
            "views": {
                "exec": "",
                "detailed": "",
                "bullets": [],
            },
            "highlights": [],
            "action_items": [],
            "decision_log": [],
            "sentiment": {
                "overall": "unclear",
                "signals": [],
                "confidence": "low",
            },
            "supportive_questions": [],
            "open_questions": [],
            "notes": [],
        }

    def assistant(self):
        """
        Live incremental assistant.
        """
        return VoiceLiveAssistant()

    def render(self, state: Dict[str, Any], *, transcript: Optional[str] = None) -> str:
        """
        Render intermediate/live markdown.
        """
        return default_markdown_service.render(state, mode="live", transcript=transcript)


    def finalizer(self):
        """
        Final cleanup/polish assistant.
        """
        return VoiceLiveCleanupAssistant()


# Singleton instance (used by registry)
default_meeting_profile = _DefaultMeetingProfile(
    id=_DefaultMeetingProfile.id,
    name=_DefaultMeetingProfile.name,
)
