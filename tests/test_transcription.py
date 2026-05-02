import base64

import pytest

from whatsapp_task_agent.evolution import NormalizedEvolutionEvent
from whatsapp_task_agent.transcription import AudioTranscriptionError, _audio_bytes_from_event


def test_audio_bytes_from_base64_payload() -> None:
    audio = b"fake-audio"
    event = NormalizedEvolutionEvent(
        from_phone="+5511999999999",
        message_type="audio",
        media_base64=base64.b64encode(audio).decode(),
    )

    assert _audio_bytes_from_event(event) == audio


def test_audio_bytes_rejects_missing_media() -> None:
    event = NormalizedEvolutionEvent(
        from_phone="+5511999999999",
        message_type="audio",
    )

    with pytest.raises(AudioTranscriptionError, match="audio_payload_has_no_media"):
        _audio_bytes_from_event(event)


def test_build_transcription_glossary_includes_context_terms() -> None:
    from whatsapp_task_agent.transcription import build_transcription_glossary, _build_whisper_prompt

    glossary = build_transcription_glossary(
        team_members=[
            {"name": "Matheus Martins", "job_title": "Dev IA"},
            {"name": "Leo Silva", "job_title": "Desenvolvedor"},
        ],
        clients=[{"name": "Dairy"}, {"name": "NanoCare"}],
        company_name="Commandix",
    )

    assert "Commandix" in glossary
    assert "Matheus Martins" in glossary
    assert "Matheus" in glossary
    assert "Dev IA" in glossary
    assert "Dairy" in glossary

    prompt = _build_whisper_prompt(glossary)
    assert "Nomes e termos da empresa" in prompt
    assert "Commandix" in prompt
    assert "Dairy" in prompt