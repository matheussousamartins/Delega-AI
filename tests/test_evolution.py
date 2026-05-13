from fastapi.testclient import TestClient

from whatsapp_task_agent.api import app
from whatsapp_task_agent.evolution import normalize_evolution_payload
from whatsapp_task_agent.settings import settings
from whatsapp_task_agent.transcription import AudioTranscriptionError


def test_normalizes_evolution_text_payload() -> None:
    event = normalize_evolution_payload(
        {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "id": "abc-123",
                },
                "message": {
                    "conversation": "minhas tarefas",
                },
                "messageType": "conversation",
            },
        }
    )

    assert event.from_phone == "+5511999999999"
    assert event.provider_message_id == "abc-123"
    assert event.message == "minhas tarefas"
    assert event.message_type == "text"
    assert event.from_me is False


def test_evolution_audio_payload_without_media_returns_safe_failure() -> None:
    client = TestClient(app)

    response = client.post(
        "/webhooks/evolution",
        json={
            "data": {
                "key": {
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "id": "audio-123",
                },
                "message": {
                    "audioMessage": {},
                },
                "messageType": "audioMessage",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "transcription_failed"
    assert body["normalized"]["message_type"] == "audio"


def test_evolution_audio_payload_transcribes_and_sends_reply(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(settings, "evolution_send_enabled", True)

    class FakeClient:
        def send_text(self, number, text):
            sent.append({"number": number, "text": text})
            return {"status": "PENDING"}

    monkeypatch.setattr("whatsapp_task_agent.api.build_evolution_client", lambda: FakeClient())
    monkeypatch.setattr("whatsapp_task_agent.api.transcribe_evolution_audio", lambda event, glossary=None: "minhas tarefas")

    client = TestClient(app)
    response = client.post(
        "/webhooks/evolution",
        json={
            "data": {
                "key": {
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "id": "audio-456",
                },
                "message": {
                    "audioMessage": {
                        "mimetype": "audio/ogg",
                        "url": "https://example.com/audio.ogg",
                    },
                },
                "messageType": "audioMessage",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "audio_processed"
    assert body["normalized"]["transcription"] == "minhas tarefas"
    assert body["outbound_status"] == "sent"
    assert len(sent) == 1
    assert sent[0]["number"] == "+5511999999999"
    assert "tarefas pendentes" in sent[0]["text"]
    assert "minhas tarefas" not in sent[0]["text"].lower()


def test_evolution_audio_transcription_error_sends_safe_reply(monkeypatch) -> None:
    sent = {}
    monkeypatch.setattr(settings, "evolution_send_enabled", True)

    class FakeClient:
        def send_text(self, number, text):
            sent["number"] = number
            sent["text"] = text
            return {"status": "PENDING"}

    monkeypatch.setattr("whatsapp_task_agent.api.build_evolution_client", lambda: FakeClient())

    def fail_transcription(event, glossary=None):
        raise AudioTranscriptionError("unsupported_or_corrupted_audio")

    monkeypatch.setattr("whatsapp_task_agent.api.transcribe_evolution_audio", fail_transcription)

    client = TestClient(app)
    response = client.post(
        "/webhooks/evolution",
        json={
            "data": {
                "key": {
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "id": "audio-789",
                },
                "message": {
                    "audioMessage": {
                        "mimetype": "audio/ogg",
                        "url": "https://example.com/audio.ogg",
                    },
                },
                "messageType": "audioMessage",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "transcription_failed"
    assert body["outbound_status"] == "sent"
    assert "Webhook Base64" in sent["text"]


def test_evolution_list_data_payload_is_ignored_without_500() -> None:
    client = TestClient(app)

    response = client.post(
        "/webhooks/evolution",
        json={
            "event": "chats.update",
            "instance": "Delega AI",
            "data": [
                {
                    "remoteJid": "109183118659821@lid",
                    "instanceId": "e62f06cd-b534-4fcf-aaf4-6382e8878b7d",
                }
            ],
            "sender": "551153049052@s.whatsapp.net",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["normalized"]["from_phone"] == "+109183118659821"


def test_evolution_from_me_payload_is_ignored_without_reply() -> None:
    client = TestClient(app)

    response = client.post(
        "/webhooks/evolution",
        json={
            "event": "messages.upsert",
            "data": {
                "key": {
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "id": "sent-by-bot",
                    "fromMe": True,
                },
                "message": {"conversation": "resposta enviada"},
                "messageType": "conversation",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored_from_me"
    assert body["normalized"]["from_me"] is True


def test_evolution_processed_message_sends_reply(monkeypatch) -> None:
    sent = {}
    monkeypatch.setattr(settings, "evolution_send_enabled", True)

    class FakeClient:
        def send_text(self, number, text):
            sent["number"] = number
            sent["text"] = text
            return {"status": "PENDING"}

    monkeypatch.setattr("whatsapp_task_agent.api.build_evolution_client", lambda: FakeClient())

    client = TestClient(app)
    response = client.post(
        "/webhooks/evolution",
        json={
            "event": "messages.upsert",
            "data": {
                "key": {
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "id": "abc-456",
                    "fromMe": False,
                },
                "message": {"conversation": "minhas tarefas"},
                "messageType": "conversation",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processed"
    assert body["outbound_status"] == "sent"
    assert sent["number"] == "+5511999999999"
    assert sent["text"]

