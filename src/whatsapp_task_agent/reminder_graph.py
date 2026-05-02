from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langgraph.graph import END, START, StateGraph

from whatsapp_task_agent.evolution_client import build_evolution_client
from whatsapp_task_agent.logger import get_logger
from whatsapp_task_agent.schemas import OutboundReminder, ReminderCandidate, ReminderKind, ReminderState
from whatsapp_task_agent.settings import settings
from whatsapp_task_agent.store import store

logger = get_logger(__name__)

APP_TIMEZONE = ZoneInfo("America/Sao_Paulo")


def prepare_window(state: ReminderState) -> ReminderState:
    now = state.get("now") or datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=APP_TIMEZONE)

    return {
        "now": now,
        "window_minutes": state.get("window_minutes", 60),
    }


def find_candidates(state: ReminderState) -> ReminderState:
    candidates = store.list_reminder_candidates(
        now=state["now"],
        window_minutes=state["window_minutes"],
    )
    logger.info("reminder_candidates_found", extra={"count": len(candidates)})
    return {"candidates": candidates}


def claim_candidates(state: ReminderState) -> ReminderState:
    candidates = state.get("candidates", [])

    if _is_quiet_hours(state["now"]):
        local_now = state["now"].astimezone(_quiet_tz())
        logger.info(
            "reminder_quiet_hours_active",
            extra={"local_hour": local_now.hour, "skipped": len(candidates)},
        )
        return {"claimed": []}

    claimed = store.claim_reminder_candidates(candidates)
    skipped = len(candidates) - len(claimed)
    if skipped:
        logger.info("reminder_candidates_skipped", extra={"skipped": skipped, "reason": "already_claimed"})
    return {"claimed": claimed}


def format_outbound_messages(state: ReminderState) -> ReminderState:
    outbound = [_format_candidate(candidate) for candidate in state.get("claimed", [])]
    return {"outbound": outbound}


def mark_reminders(state: ReminderState) -> ReminderState:
    send_results = _send_outbound_messages(state.get("outbound", []))
    sent_keys = {
        (item["task_id"], item["kind"])
        for item in send_results
        if item["status"] == "sent"
    }

    for candidate in state.get("claimed", []):
        if (str(candidate.task_id), candidate.kind.value) in sent_keys:
            store.mark_reminder_sent(candidate, state["now"])

    claimed_count = len(state.get("claimed", []))
    skipped = len(state.get("candidates", [])) - claimed_count
    sent = sum(1 for item in send_results if item["status"] == "sent")
    failed = sum(1 for item in send_results if item["status"] == "failed")
    dry_run = sum(1 for item in send_results if item["status"] == "dry_run")

    quiet = _is_quiet_hours(state["now"])
    logger.info(
        "reminder_job_complete",
        extra={
            "candidates": len(state.get("candidates", [])),
            "claimed": claimed_count,
            "skipped_already_claimed": skipped,
            "quiet_hours_active": quiet,
            "sent": sent,
            "failed": failed,
            "dry_run": dry_run,
        },
    )
    if failed:
        logger.warning("reminder_send_failures", extra={"failed": failed, "results": send_results})

    return {
        "result": {
            "candidates": len(state.get("candidates", [])),
            "claimed": claimed_count,
            "skipped_already_claimed": skipped,
            "quiet_hours_active": quiet,
            "outbound": len(state.get("outbound", [])),
            "send_enabled": settings.evolution_send_enabled,
            "sent": sent,
            "failed": failed,
            "dry_run": dry_run,
            "send_results": send_results,
        }
    }


def build_reminder_graph():
    graph = StateGraph(ReminderState)
    graph.add_node("prepare_window", prepare_window)
    graph.add_node("find_candidates", find_candidates)
    graph.add_node("claim_candidates", claim_candidates)
    graph.add_node("format_outbound_messages", format_outbound_messages)
    graph.add_node("mark_reminders", mark_reminders)

    graph.add_edge(START, "prepare_window")
    graph.add_edge("prepare_window", "find_candidates")
    graph.add_edge("find_candidates", "claim_candidates")
    graph.add_edge("claim_candidates", "format_outbound_messages")
    graph.add_edge("format_outbound_messages", "mark_reminders")
    graph.add_edge("mark_reminders", END)

    return graph.compile()


def _format_candidate(candidate: ReminderCandidate) -> OutboundReminder:
    due_at = _format_due_at(candidate.due_at)

    if candidate.kind == ReminderKind.overdue:
        message = (
            "⚠️ Tarefa atrasada\n\n"
            f"📌 {candidate.title}\n"
            f"⏰ Venceu em: {due_at}\n\n"
            "Para resolver:\n"
            f"concluir {candidate.title}\n"
            f"Ou: reagendar {candidate.title} para amanha as 10"
        )
    elif candidate.kind == ReminderKind.due_now:
        message = (
            "⏰ Essa tarefa vence agora\n\n"
            f"📌 {candidate.title}\n"
            f"⏰ Prazo: {due_at}\n\n"
            "Quando terminar:\n"
            f"concluir {candidate.title}"
        )
    else:
        message = (
            "⏰ Lembrete de prazo\n\n"
            f"📌 {candidate.title}\n"
            f"⏰ Prazo: {due_at}\n\n"
            "Se precisar mudar:\n"
            f"reagendar {candidate.title} para amanha as 10"
        )

    return OutboundReminder(
        task_id=candidate.task_id,
        kind=candidate.kind,
        to_phone=candidate.assignee_phone,
        message=message,
    )


def _send_outbound_messages(messages: list[OutboundReminder]) -> list[dict]:
    if not messages:
        return []

    if not settings.evolution_send_enabled:
        return [
            {
                "status": "dry_run",
                "task_id": str(message.task_id),
                "kind": message.kind.value,
                "to_phone": message.to_phone,
            }
            for message in messages
        ]

    client = build_evolution_client()
    if client is None:
        return [
            {
                "status": "failed",
                "task_id": str(message.task_id),
                "kind": message.kind.value,
                "to_phone": message.to_phone,
                "error": "evolution_client_not_configured",
            }
            for message in messages
        ]

    results = []
    for message in messages:
        try:
            response = client.send_text(number=message.to_phone, text=message.message)
            results.append(
                {
                    "status": "sent",
                    "task_id": str(message.task_id),
                    "kind": message.kind.value,
                    "to_phone": message.to_phone,
                    "provider_response": response,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "status": "failed",
                    "task_id": str(message.task_id),
                    "kind": message.kind.value,
                    "to_phone": message.to_phone,
                    "error": str(exc),
                }
            )
    return results


def _format_due_at(value: datetime) -> str:
    due_at = value
    if due_at.tzinfo is not None:
        due_at = due_at.astimezone(APP_TIMEZONE)
    return due_at.strftime("%d/%m as %H:%M")


def _quiet_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.quiet_hours_timezone)
    except (ZoneInfoNotFoundError, KeyError):
        return APP_TIMEZONE


def _is_quiet_hours(now: datetime) -> bool:
    if not settings.quiet_hours_enabled:
        return False

    local_hour = now.astimezone(_quiet_tz()).hour
    start = settings.quiet_hours_start
    end = settings.quiet_hours_end

    if start == end:
        return False

    if start > end:
        # Wraps midnight: e.g. 22h–07h
        return local_hour >= start or local_hour < end
    else:
        # Doesn't wrap: e.g. 10h–18h
        return start <= local_hour < end


reminder_graph = build_reminder_graph()
