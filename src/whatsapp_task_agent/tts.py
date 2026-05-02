from openai import OpenAI, OpenAIError

from whatsapp_task_agent.logger import get_logger
from whatsapp_task_agent.settings import settings

logger = get_logger(__name__)


def generate_tts(text: str) -> bytes | None:
    if not settings.tts_enabled or not settings.openai_api_key:
        return None
    client = OpenAI(api_key=settings.openai_api_key)
    try:
        response = client.audio.speech.create(
            model="tts-1",
            voice=settings.tts_voice,
            input=text,
            response_format="mp3",
        )
        logger.info("tts_generated", extra={"chars": len(text), "voice": settings.tts_voice})
        return response.content
    except OpenAIError as exc:
        logger.error("tts_failed", extra={"exc_type": type(exc).__name__}, exc_info=True)
        return None
    except Exception as exc:
        logger.error("tts_unexpected_error", extra={"exc_type": type(exc).__name__}, exc_info=True)
        return None
