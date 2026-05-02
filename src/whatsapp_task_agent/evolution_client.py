import base64
from typing import Any
from urllib.parse import quote

import httpx

from whatsapp_task_agent.settings import settings


class EvolutionClient:
    def __init__(
        self,
        base_url: str,
        instance_name: str,
        api_key: str,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.instance_name = instance_name
        self.api_key = api_key
        self.timeout = timeout

    def send_text(self, number: str, text: str) -> dict[str, Any]:
        instance = quote(self.instance_name, safe="")
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            response = client.post(
                f"{self.base_url}/message/sendText/{instance}",
                headers={
                    "Content-Type": "application/json",
                    "apikey": self.api_key,
                },
                json={
                    "number": _normalize_outbound_number(number),
                    "text": text,
                },
            )
        response.raise_for_status()
        return response.json()

    def send_audio(self, number: str, audio_bytes: bytes, mimetype: str = "audio/mpeg") -> dict[str, Any]:
        instance = quote(self.instance_name, safe="")
        audio_b64 = base64.b64encode(audio_bytes).decode()
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            response = client.post(
                f"{self.base_url}/message/sendMedia/{instance}",
                headers={
                    "Content-Type": "application/json",
                    "apikey": self.api_key,
                },
                json={
                    "number": _normalize_outbound_number(number),
                    "mediatype": "audio",
                    "mimetype": mimetype,
                    "media": audio_b64,
                    "fileName": "resposta.mp3",
                },
            )
        response.raise_for_status()
        return response.json()

    def send_presence(self, number: str, presence: str = "composing", delay_ms: int = 1800) -> dict[str, Any]:
        """Send a WhatsApp presence update (e.g. "composing" = typing indicator).

        Evolution API v2+ uses a flat payload; older builds wrapped these fields
        inside an `options` object.  We try the flat format first and fall back
        silently — callers treat the entire call as best-effort.
        """
        instance = quote(self.instance_name, safe="")
        payload = {
            "number": _normalize_outbound_number(number),
            "delay": delay_ms,
            "presence": presence,
        }
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            response = client.post(
                f"{self.base_url}/chat/sendPresence/{instance}",
                headers={"Content-Type": "application/json", "apikey": self.api_key},
                json=payload,
            )
            if response.status_code in (400, 422):
                # Older Evolution builds expect options-wrapped payload.
                response = client.post(
                    f"{self.base_url}/chat/sendPresence/{instance}",
                    headers={"Content-Type": "application/json", "apikey": self.api_key},
                    json={
                        "number": payload["number"],
                        "options": {"delay": delay_ms, "presence": presence},
                    },
                )
        response.raise_for_status()
        if not response.content:
            return {"status": "ok"}
        return response.json()


def build_evolution_client() -> EvolutionClient | None:
    if not (
        settings.evolution_api_base_url
        and settings.evolution_instance_name
        and settings.evolution_api_key
    ):
        return None

    return EvolutionClient(
        base_url=settings.evolution_api_base_url,
        instance_name=settings.evolution_instance_name,
        api_key=settings.evolution_api_key,
    )


def _normalize_outbound_number(number: str) -> str:
    return "".join(char for char in number if char.isdigit())
