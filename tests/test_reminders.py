from datetime import datetime, timedelta

from whatsapp_task_agent.reminder_graph import _format_candidate, prepare_window
from whatsapp_task_agent.schemas import ReminderKind
from whatsapp_task_agent.store import InMemoryTaskStore
from whatsapp_task_agent.tools import _parse_datetime


def test_memory_store_returns_due_soon_reminder_candidate_once() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    now = datetime(2026, 4, 29, 10, 0)
    task = store.create_task(
        company_id=context["company_id"],
        created_by=context["user_id"],
        title="Enviar proposta",
        assignee_name="Joao",
        due_at=now + timedelta(minutes=45),
    )

    candidates = store.list_reminder_candidates(now=now, window_minutes=60)

    assert len(candidates) == 1
    assert candidates[0].task_id == task.id
    assert candidates[0].kind == ReminderKind.due_soon
    assert candidates[0].assignee_phone == "+5511988888888"

    store.mark_reminder_sent(candidates[0], now)

    assert store.list_reminder_candidates(now=now, window_minutes=60) == []


def test_formats_overdue_follow_up_message() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    now = datetime(2026, 4, 29, 10, 0)
    store.create_task(
        company_id=context["company_id"],
        created_by=context["user_id"],
        title="Enviar proposta",
        assignee_name="Joao",
        due_at=now - timedelta(minutes=10),
    )

    candidate = store.list_reminder_candidates(now=now)[0]
    outbound = _format_candidate(candidate)

    assert outbound.kind == ReminderKind.overdue
    assert outbound.to_phone == "+5511988888888"
    assert "⚠️ Tarefa atrasada" in outbound.message
    assert "📌 Enviar proposta" in outbound.message
    assert "concluir Enviar proposta" in outbound.message
    assert "reagendar Enviar proposta para amanha as 10" in outbound.message


def test_formats_due_soon_message_with_actionable_command() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    now = datetime(2026, 4, 29, 10, 0)
    store.create_task(
        company_id=context["company_id"],
        created_by=context["user_id"],
        title="Revisar contrato",
        assignee_name="Joao",
        due_at=now + timedelta(minutes=45),
    )

    candidate = store.list_reminder_candidates(now=now)[0]
    outbound = _format_candidate(candidate)

    assert outbound.kind == ReminderKind.due_soon
    assert "⏰ Lembrete de prazo" in outbound.message
    assert "📌 Revisar contrato" in outbound.message
    assert "reagendar Revisar contrato para amanha as 10" in outbound.message


def test_prepare_window_accepts_iso_datetime_from_api_payload() -> None:
    state = prepare_window({"now": "2026-04-29T10:00:00-03:00", "window_minutes": 30})

    assert state["now"].isoformat() == "2026-04-29T10:00:00-03:00"
    assert state["window_minutes"] == 30


def test_user_due_date_without_timezone_is_treated_as_sao_paulo_time() -> None:
    due_at = _parse_datetime("2026-04-29T18:47:00")

    assert due_at is not None
    assert due_at.isoformat() == "2026-04-29T18:47:00-03:00"
