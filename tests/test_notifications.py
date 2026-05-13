from datetime import datetime, timedelta

from whatsapp_task_agent import api
from whatsapp_task_agent.store import InMemoryTaskStore


def test_notification_outbox_retries_failed_delivery() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    task = store.create_task(
        company_id=context["company_id"],
        created_by=context["user_id"],
        title="Enviar proposta",
        assignee_name="Joao",
    )
    now = datetime.now() + timedelta(minutes=1)

    notification = store.enqueue_notification(
        company_id=context["company_id"],
        task_id=str(task.id),
        recipient_user_id=str(task.assigned_to),
        recipient_phone="+5511988888888",
        notification_type="task_delegated",
        message="Nova tarefa",
    )

    assert store.list_due_notifications(now=now) == [notification]

    store.mark_notification_failed(
        notification_id=notification["id"],
        error="temporary_failure",
        failed_at=now,
    )

    assert store.list_due_notifications(now=now) == []
    assert store.list_due_notifications(now=now + timedelta(minutes=3))[0]["attempts"] == 1


def test_notification_outbox_stops_retrying_after_max_attempts() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    task = store.create_task(
        company_id=context["company_id"],
        created_by=context["user_id"],
        title="Enviar proposta",
        assignee_name="Joao",
    )
    now = datetime.now() + timedelta(minutes=1)
    notification = store.enqueue_notification(
        company_id=context["company_id"],
        task_id=str(task.id),
        recipient_user_id=str(task.assigned_to),
        recipient_phone="+5511988888888",
        notification_type="task_delegated",
        message="Nova tarefa",
    )

    store.mark_notification_failed(
        notification_id=notification["id"],
        error="permanent_failure",
        failed_at=now,
        max_attempts=1,
    )

    assert store.list_due_notifications(now=now + timedelta(hours=1)) == []


def test_invite_acceptance_notification_goes_to_inviter(monkeypatch) -> None:
    sent_messages = []

    def fake_send_text(to_phone: str, reply: str) -> dict:
        sent_messages.append({"to_phone": to_phone, "reply": reply})
        return {"status": "sent", "provider_response": {}}

    monkeypatch.setattr(api, "_send_evolution_reply", fake_send_text)

    result = api._send_invite_accepted_notification(
        {
            "result": {
                "should_notify_inviter": True,
                "company_id": "company-1",
                "company_name": "Commandix",
                "user_name": "Luiz",
                "job_title": "Desenvolvedor",
                "invited_by": "owner-1",
                "invited_by_phone": "+5541999999999",
            }
        }
    )

    assert result["status"] == "sent"
    assert sent_messages == [
        {
            "to_phone": "+5541999999999",
            "reply": (
                "✅ Luiz aceitou o convite e agora faz parte da Commandix.\n"
                "Cargo: Desenvolvedor\n\n"
                "Vocês já podem trocar delegações pelo Delega AI."
            ),
        }
    ]

