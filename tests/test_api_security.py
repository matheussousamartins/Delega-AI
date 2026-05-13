from fastapi.testclient import TestClient

from whatsapp_task_agent.api import (
    _format_delegation_notification,
    _format_invite_notification,
    _format_task_update_notification,
    _send_delegation_notification,
    _send_evolution_reply,
    _send_task_update_notification,
    app,
)
from whatsapp_task_agent.settings import settings


def test_reminder_job_requires_secret_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "reminder_job_secret", "secret-test")
    client = TestClient(app)

    response = client.post("/jobs/reminders", json={})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_job_secret"


def test_notification_retry_job_requires_secret_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "reminder_job_secret", "secret-test")
    client = TestClient(app)

    response = client.post("/jobs/notifications", json={})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_job_secret"


def test_jobs_require_secret_configuration_in_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "reminder_job_secret", None)
    client = TestClient(app)

    response = client.post("/jobs/reminders", json={})

    assert response.status_code == 503
    assert response.json()["detail"] == "reminder_job_secret_not_configured"


def test_evolution_webhook_requires_secret_configuration_in_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "evolution_webhook_secret", None)
    client = TestClient(app)

    response = client.post("/webhooks/evolution", json={})

    assert response.status_code == 503
    assert response.json()["detail"] == "evolution_webhook_secret_not_configured"


def test_evolution_webhook_accepts_secret_from_query_string(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "evolution_webhook_secret", "query-secret")
    client = TestClient(app)

    response = client.post("/webhooks/evolution?apikey=query-secret", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_manual_whatsapp_webhook_requires_observe_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "observe_api_key", None)
    client = TestClient(app)

    response = client.post(
        "/webhooks/whatsapp",
        json={"from_phone": "+5511999999999", "message": "minhas tarefas"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "observe_disabled"


def test_send_evolution_reply_dry_run_when_send_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "evolution_send_enabled", False)
    monkeypatch.setattr(
        "whatsapp_task_agent.api.build_evolution_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be built")),
    )

    result = _send_evolution_reply("+5511999999999", "teste")

    assert result == {"status": "dry_run"}


def test_delegation_notification_is_skipped_for_self_assigned_task() -> None:
    result = _send_delegation_notification(
        {
            "result": {
                "should_notify_assignee": False,
                "assignee_phone": "+5511999999999",
            }
        }
    )

    assert result == {"status": "skipped"}


def test_formats_delegation_notification() -> None:
    message = _format_delegation_notification(
        {
            "title": "Enviar proposta",
            "created_by_name": "Matheus",
            "due_at": "2026-04-30T18:00:00-03:00",
        }
    )

    assert "Nova tarefa para você." in message
    assert "Tarefa: Enviar proposta" in message
    assert "Criada por: Matheus" in message
    assert "Prazo: 30/04 as 18:00" in message
    assert "minhas tarefas" in message


def test_formats_invite_notification() -> None:
    message = _format_invite_notification(
        {
            "company_name": "Commandix",
            "invited_by_name": "Matheus",
            "job_title": "Desenvolvedor",
        }
    )

    assert "convite" in message.lower()
    assert "Commandix" in message
    assert "Matheus" in message
    assert "Desenvolvedor" in message
    assert "aceitar convite" in message
    assert "recusar" in message



def test_formats_task_update_notification() -> None:
    message = _format_task_update_notification(
        {
            "actor_name": "Leo",
            "title": "Fazer o deploy da Nanocare",
            "task_update_type": "rescheduled",
            "due_at": "2026-05-02T18:00:00-03:00",
        }
    )

    assert "Leo atualizou o prazo" in message
    assert "Fazer o deploy da Nanocare" in message
    assert "Novo prazo: 02/05 as 18:00" in message


def test_task_update_notification_goes_to_creator(monkeypatch) -> None:
    sent_messages = []

    def fake_send_text(to_phone: str, reply: str) -> dict:
        sent_messages.append({"to_phone": to_phone, "reply": reply})
        return {"status": "sent", "provider_response": {}}

    monkeypatch.setattr("whatsapp_task_agent.api._send_evolution_reply", fake_send_text)

    result = _send_task_update_notification(
        {
            "context": {"company_id": "company-1"},
            "result": {
                "should_notify_task_creator": True,
                "created_by": "owner-1",
                "created_by_phone": "+5511999999999",
                "actor_name": "Leo",
                "task_id": "task-1",
                "title": "Fazer o deploy da Nanocare",
                "task_update_type": "started",
            },
        }
    )

    assert result["status"] == "sent"
    assert sent_messages[0]["to_phone"] == "+5511999999999"
    assert "Leo começou a tarefa" in sent_messages[0]["reply"]
    assert "Fazer o deploy da Nanocare" in sent_messages[0]["reply"]


def test_observe_endpoint_disabled_when_no_key_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "observe_api_key", None)
    client = TestClient(app)

    response = client.get("/observe/messages")

    assert response.status_code == 403
    assert response.json()["detail"] == "observe_disabled"


def test_observe_endpoint_rejects_wrong_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "observe_api_key", "secret-obs-key")
    client = TestClient(app)

    response = client.get("/observe/messages", headers={"x-observe-key": "wrong-key"})

    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_observe_key"


def test_observe_endpoint_returns_messages_with_valid_key(monkeypatch, isolated_store) -> None:
    monkeypatch.setattr(settings, "observe_api_key", "secret-obs-key")
    from datetime import datetime, timezone
    isolated_store.messages.append({
        "ts": "2026-05-01T20:00:00+00:00",
        "company_id": str(isolated_store.company_id),
        "user_id": str(isolated_store.owner_id),
        "from_phone": "+5511999999999",
        "provider_message_id": "obs-test-1",
        "body": "Joao, validar integração da Dairy amanha as 15",
        "parsed": {"intent": "task", "action": "create_task", "confidence": 91, "params": {}},
        "response_body": "Tarefa criada para você.",
        "error": None,
    })
    client = TestClient(app)

    response = client.get("/observe/messages", headers={"x-observe-key": "secret-obs-key"})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    entry = body["messages"][0]
    assert entry["ts"] == "2026-05-01T20:00:00+00:00"
    assert entry["phone_tail"] == "9999"
    assert entry["intent"] == "task"
    assert entry["action"] == "create_task"
    assert entry["confidence"] == 91
    assert "Joao" in entry["message"]
    assert entry["error"] is None
