"""Unit tests for transcription/TTS adapters and the cascaded voice pipeline
(Phases 3 & 4). Fakes stand in for the SDK clients and providers — no network."""
from __future__ import annotations

import asyncio

from services.providers.base import ChatProvider, ChatResult, SpeechProvider, TranscriptionProvider
from services.providers.openai_compatible_provider import (
    OpenAICompatibleTranscriptionProvider,
    OpenAICompatibleSpeechProvider,
)
from services.providers.voice_pipeline import CascadedVoicePipeline


# ── transcription adapter ─────────────────────────────────────────────────────
class _Transcription:
    def __init__(self, text):
        self.text = text


class _FakeTranscriptions:
    def __init__(self, text):
        self.text = text
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _Transcription(self.text)


class _FakeAudioStt:
    def __init__(self, text):
        self.transcriptions = _FakeTranscriptions(text)


class _FakeSttClient:
    def __init__(self, text):
        self.audio = _FakeAudioStt(text)


def test_transcription_adapter():
    client = _FakeSttClient("hello world")
    p = OpenAICompatibleTranscriptionProvider(base_url="http://x/v1", default_model="whisper-1", client=client)
    out = asyncio.run(p.transcribe(b"RIFF....", language="en", filename="a.wav"))
    assert out == "hello world"
    kw = client.audio.transcriptions.last_kwargs
    assert kw["model"] == "whisper-1"
    assert kw["file"] == ("a.wav", b"RIFF....")
    assert kw["language"] == "en"


# ── TTS adapter ───────────────────────────────────────────────────────────────
class _Speech:
    def __init__(self, content):
        self.content = content


class _FakeSpeech:
    def __init__(self, content):
        self.content = content
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _Speech(self.content)


class _FakeAudioTts:
    def __init__(self, content):
        self.speech = _FakeSpeech(content)


class _FakeTtsClient:
    def __init__(self, content):
        self.audio = _FakeAudioTts(content)


def test_tts_adapter_returns_bytes():
    client = _FakeTtsClient(b"\x00\x01audio")
    p = OpenAICompatibleSpeechProvider(base_url="http://x/v1", default_model="tts-1", default_voice="alloy", client=client)
    audio = asyncio.run(p.synthesize("hi there", voice="nova"))
    assert audio == b"\x00\x01audio"
    assert client.audio.speech.last_kwargs["voice"] == "nova"
    assert client.audio.speech.last_kwargs["input"] == "hi there"


# ── cascaded pipeline ─────────────────────────────────────────────────────────
class FakeSTT(TranscriptionProvider):
    def __init__(self, text):
        self._text = text

    async def transcribe(self, audio, *, model=None, language=None, filename="audio.wav"):
        return self._text


class FakeChat(ChatProvider):
    def __init__(self, reply):
        self._reply = reply
        self.seen = None

    async def chat(self, user_input, *, model=None, instructions=None, **kwargs):
        self.seen = (user_input, model, instructions)
        return ChatResult(text=self._reply, provider="fake")


class FakeTTS(SpeechProvider):
    async def synthesize(self, text, *, model=None, voice=None):
        return b"AUDIO:" + text.encode()


def test_cascaded_pipeline_happy_path():
    chat = FakeChat("the answer is 42")
    pipe = CascadedVoicePipeline(FakeSTT("what is the answer"), chat, FakeTTS(),
                                 system="be terse", chat_model="claude-opus-4-8", voice="nova")
    turn = asyncio.run(pipe.process_utterance(b"audio"))
    assert turn.transcript == "what is the answer"
    assert turn.response_text == "the answer is 42"
    assert turn.audio_out == b"AUDIO:the answer is 42"
    # chat received the transcript + configured model/system
    assert chat.seen == ("what is the answer", "claude-opus-4-8", "be terse")


def test_cascaded_pipeline_empty_transcript_skips():
    pipe = CascadedVoicePipeline(FakeSTT("   "), FakeChat("x"), FakeTTS())
    turn = asyncio.run(pipe.process_utterance(b"silence"))
    assert turn.transcript == "" and turn.response_text == "" and turn.audio_out == b""


def test_cascaded_pipeline_empty_reply_no_tts():
    pipe = CascadedVoicePipeline(FakeSTT("hi"), FakeChat("  "), FakeTTS())
    turn = asyncio.run(pipe.process_utterance(b"audio"))
    assert turn.transcript == "hi" and turn.response_text == "" and turn.audio_out == b""
