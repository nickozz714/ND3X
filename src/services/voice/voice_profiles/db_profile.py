"""Adapter that turns a DB-defined meeting profile into a runtime profile object
(duck-typed VoiceLiveProfile). It reuses the default meeting flow (state schema,
renderer, finalizer) and only overrides the live assistant's instructions with the
profile's instructions/language/output guidance.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from assistants.voice.voice_live_assistant import VoiceLiveAssistant
from services.voice.voice_profiles.default_meeting import default_meeting_profile


class DbMeetingProfile:
    """Duck-typed to VoiceLiveProfile (id, name, empty_state, assistant, render, finalizer)."""

    def __init__(self, *, id: str, name: str, instructions: Optional[str] = None,
                 language: Optional[str] = None, output_template: Optional[str] = None):
        self.id = id
        self.name = name
        self._instructions = instructions
        self._language = language
        self._output = output_template

    def empty_state(self) -> Dict[str, Any]:
        return default_meeting_profile.empty_state()

    def assistant(self):
        a = VoiceLiveAssistant()
        extra = []
        if self._instructions:
            extra.append(self._instructions.strip())
        if self._language:
            extra.append(f"Produce all output in this language: {self._language}.")
        if self._output:
            extra.append("Desired output structure:\n" + self._output.strip())
        if extra:
            a.instructions = a.instructions + "\n\n## Profile overrides\n" + "\n\n".join(extra)
        return a

    def render(self, state: Dict[str, Any], *, transcript: Optional[str] = None) -> str:
        return default_meeting_profile.render(state, transcript=transcript)

    def finalizer(self):
        return default_meeting_profile.finalizer()
