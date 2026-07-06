"""
services/providers/voice_pipeline.py

Provider-agnostic cascaded voice: STT → chat → TTS. This is the fallback path for
providers without a native realtime API (Claude, local). Native full-duplex
(OpenAI Realtime, Gemini Live) is handled separately by the existing realtime
client; this pipeline is what makes voice work with any chat provider.

Latency note: cascaded ≠ true full-duplex — there's a transcribe→think→speak
round trip per utterance. The UI surfaces this.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from component.logging import get_logger
from services.providers.base import ChatProvider, SpeechProvider, TranscriptionProvider

log = get_logger(__name__)


@dataclass
class VoiceTurn:
    transcript: str
    response_text: str
    audio_out: bytes


class CascadedVoicePipeline:
    def __init__(
        self,
        stt: TranscriptionProvider,
        chat: ChatProvider,
        tts: SpeechProvider,
        *,
        system: Optional[str] = None,
        chat_model: Optional[str] = None,
        voice: Optional[str] = None,
    ):
        self._stt = stt
        self._chat = chat
        self._tts = tts
        self._system = system
        self._chat_model = chat_model
        self._voice = voice

    async def process_utterance(self, audio: bytes, *, language: Optional[str] = None) -> VoiceTurn:
        transcript = (await self._stt.transcribe(audio, language=language) or "").strip()
        if not transcript:
            return VoiceTurn(transcript="", response_text="", audio_out=b"")

        result = await self._chat.chat(transcript, model=self._chat_model, instructions=self._system)
        response_text = (result.text or "").strip()
        if not response_text:
            return VoiceTurn(transcript=transcript, response_text="", audio_out=b"")

        audio_out = await self._tts.synthesize(response_text, voice=self._voice)
        return VoiceTurn(transcript=transcript, response_text=response_text, audio_out=audio_out)
