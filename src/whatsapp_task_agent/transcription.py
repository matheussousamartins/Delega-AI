import base64
import tempfile
from pathlib import Path

import httpx
from openai import OpenAI, OpenAIError

from whatsapp_task_agent.evolution import NormalizedEvolutionEvent
from whatsapp_task_agent.logger import get_logger
from whatsapp_task_agent.settings import settings

logger = get_logger(__name__)

_BASE_DOMAIN_TERMS = (
    "criar tarefa, delegar, concluir, iniciar, reagendar, prazo, urgente, reuniao, "
    "proposta, contrato, revisar, cliente, pendente, atrasado, hoje, amanha, "
    "semana que vem, sem prazo, minhas tarefas, tarefas do time, resumo"
)


class AudioTranscriptionError(Exception):
    pass


def build_transcription_glossary(
    team_members: list[dict] | None = None,
    clients: list[dict] | None = None,
    company_name: str | None = None,
) -> list[str]:
    terms: list[str] = []

    def add(value: str | None) -> None:
        cleaned = (value or "").strip()
        if cleaned and cleaned not in terms:
            terms.append(cleaned)

    add(company_name)
    for member in team_members or []:
        name = member.get("name")
        add(name)
        if name:
            add(str(name).split()[0])
        add(member.get("job_title"))
    for client in clients or []:
        add(client.get("name"))
    return terms


def transcribe_evolution_audio(
    event: NormalizedEvolutionEvent,
    glossary: list[str] | None = None,
) -> str:
    if not settings.openai_api_key:
        raise AudioTranscriptionError("openai_api_key_not_configured")

    source = "base64" if event.media_base64 else "url"
    logger.info(
        "transcription_start",
        extra={"source": source, "mimetype": event.mimetype, "glossary_terms": len(glossary or [])},
    )

    audio_bytes = _audio_bytes_from_event(event)
    suffix = _suffix_from_mimetype(event.mimetype)
    prompt = _build_whisper_prompt(glossary)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(audio_bytes)
            temp_path = Path(temp_file.name)

        client = OpenAI(api_key=settings.openai_api_key)
        try:
            with temp_path.open("rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model=settings.openai_transcription_model,
                    file=audio_file,
                    language="pt",
                    prompt=prompt,
                )
        except OpenAIError as exc:
            logger.error("transcription_openai_error", extra={"exc_type": type(exc).__name__}, exc_info=True)
            raise AudioTranscriptionError("unsupported_or_corrupted_audio") from exc
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)

    text = getattr(transcription, "text", None)
    if not text:
        logger.warning("transcription_empty_result", extra={"source": source})
        raise AudioTranscriptionError("empty_transcription")

    logger.info("transcription_ok", extra={"chars": len(text)})
    return text.strip()


def _build_whisper_prompt(glossary: list[str] | None = None) -> str:
    parts = [
        "Mensagem de voz em portugues do Brasil.",
        "Sistema de gestao de tarefas empresariais por WhatsApp.",
        f"Termos comuns: {_BASE_DOMAIN_TERMS}.",
    ]
    if glossary:
        parts.append(f"Nomes e termos da empresa: {', '.join(glossary[:40])}.")
    return " ".join(parts)


def _audio_bytes_from_event(event: NormalizedEvolutionEvent) -> bytes:
    if event.media_base64:
        return _decode_base64_audio(event.media_base64)
    if event.media_url:
        return _download_audio(event.media_url)
    raise AudioTranscriptionError("audio_payload_has_no_media")


def _decode_base64_audio(value: str) -> bytes:
    if "," in value and value.strip().startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        audio_bytes = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise AudioTranscriptionError("invalid_audio_base64") from exc
    _validate_audio_size(audio_bytes)
    return audio_bytes


def _download_audio(url: str) -> bytes:
    try:
        with httpx.Client(timeout=30.0, trust_env=False, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            audio_bytes = response.content
    except Exception as exc:
        raise AudioTranscriptionError("audio_download_failed") from exc
    if content_type and not _looks_like_audio_content_type(content_type):
        raise AudioTranscriptionError("downloaded_media_is_not_audio")
    _validate_audio_size(audio_bytes)
    return audio_bytes


def _validate_audio_size(audio_bytes: bytes) -> None:
    if not audio_bytes:
        raise AudioTranscriptionError("empty_audio_file")
    if len(audio_bytes) > settings.audio_max_bytes:
        raise AudioTranscriptionError("audio_file_too_large")


def _suffix_from_mimetype(mimetype: str | None) -> str:
    if not mimetype:
        return ".ogg"
    if "mpeg" in mimetype or "mp3" in mimetype:
        return ".mp3"
    if "wav" in mimetype:
        return ".wav"
    if "mp4" in mimetype or "m4a" in mimetype:
        return ".m4a"
    if "webm" in mimetype:
        return ".webm"
    return ".ogg"


def _looks_like_audio_content_type(content_type: str) -> bool:
    normalized = content_type.lower()
    return (
        normalized.startswith("audio/")
        or "octet-stream" in normalized
        or "ogg" in normalized
        or "opus" in normalized
    )
