from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import hmac

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request

from whatsapp_task_agent.evolution import normalize_evolution_payload
from whatsapp_task_agent.evolution_client import build_evolution_client
from whatsapp_task_agent.graph import app_graph
from whatsapp_task_agent.logger import get_logger, setup_logging
from whatsapp_task_agent.postgres_store import PostgresTaskStore
from whatsapp_task_agent.reminder_graph import reminder_graph
from whatsapp_task_agent.schemas import AgentResponse, EvolutionWebhookResponse, ObserveResponse, WhatsAppInput
from whatsapp_task_agent.settings import settings
from whatsapp_task_agent.store import store
from whatsapp_task_agent.transcription import AudioTranscriptionError, build_transcription_glossary, transcribe_evolution_audio
from whatsapp_task_agent.tts import generate_tts

setup_logging(settings.app_env)
logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logger.info("app_startup", extra={"env": settings.app_env})
    yield
    if isinstance(store, PostgresTaskStore):
        store._pool.close()
        logger.info("app_shutdown", extra={"pool": "closed"})


app = FastAPI(title="Delega AI", lifespan=_lifespan)


async def _verify_evolution_webhook(
    request: Request,
    apikey: str | None = Header(default=None),
) -> None:
    secret = settings.evolution_webhook_secret
    if not secret:
        if settings.app_env == "production":
            raise HTTPException(
                status_code=503,
                detail="evolution_webhook_secret_not_configured",
            )
        return

    token = apikey or request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="invalid_webhook_secret")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _verify_observe_key(x_observe_key: str | None = Header(default=None)) -> None:
    if not settings.observe_api_key:
        raise HTTPException(status_code=403, detail="observe_disabled")
    if not x_observe_key or not hmac.compare_digest(x_observe_key, settings.observe_api_key):
        raise HTTPException(status_code=403, detail="invalid_observe_key")


@app.get("/observe/messages", response_model=ObserveResponse, dependencies=[Depends(_verify_observe_key)])
def observe_messages(limit: int = Query(default=50, ge=1, le=200)) -> ObserveResponse:
    raw = store.get_observe_messages(limit=limit)
    entries = []
    for m in raw:
        parsed = m.get("parsed") or {}
        phone = m.get("from_phone") or m.get("user_id", "")
        phone_tail = phone[-4:] if phone else "????"
        entries.append(
            {
                "ts": m.get("ts", ""),
                "phone_tail": phone_tail,
                "intent": parsed.get("intent"),
                "action": parsed.get("action"),
                "confidence": parsed.get("confidence"),
                "message": (m.get("body") or "")[:300],
                "reply": (m.get("response_body") or "")[:400],
                "error": m.get("error"),
            }
        )
    return ObserveResponse(count=len(entries), messages=entries)


@app.post(
    "/webhooks/whatsapp",
    response_model=AgentResponse,
    dependencies=[Depends(_verify_observe_key)],
)
def whatsapp_webhook(payload: WhatsAppInput) -> AgentResponse:
    state = app_graph.invoke(payload.model_dump())
    return AgentResponse(
        reply=state["reply"],
        parsed=state["parsed"],
        result=state.get("result", {}),
    )


@app.post("/webhooks/evolution", response_model=EvolutionWebhookResponse, dependencies=[Depends(_verify_evolution_webhook)])
def evolution_webhook(payload: dict[str, Any] = Body(...)) -> EvolutionWebhookResponse:
    try:
        event = normalize_evolution_payload(payload)
    except ValueError as exc:
        logger.warning("webhook_ignored", extra={"reason": str(exc)})
        return EvolutionWebhookResponse(
            status="ignored",
            reply=f"Evento ignorado: {exc}",
            normalized={},
        )

    phone_tail = event.from_phone[-4:] if event.from_phone else "????"

    if event.from_me:
        return EvolutionWebhookResponse(
            status="ignored_from_me",
            reply="Mensagem enviada pela propria instancia. Evento ignorado.",
            normalized=event.model_dump(),
        )

    if event.provider_message_id:
        cached = store.get_message_by_provider_id(event.provider_message_id)
        if cached is not None:
            logger.info("webhook_duplicate", extra={"phone_tail": phone_tail, "provider_message_id": event.provider_message_id})
            return EvolutionWebhookResponse(
                status="duplicate",
                reply=cached,
                normalized=event.model_dump(),
            )

    try:
        if event.is_audio:
            logger.info("webhook_audio", extra={"phone_tail": phone_tail})
            return _process_audio_event(event)

        if event.is_contact:
            logger.info("webhook_contact", extra={"phone_tail": phone_tail, "contact_name": event.contact_name})
            return _process_contact_event(event)

        if not event.is_text:
            return EvolutionWebhookResponse(
                status="ignored",
                reply="Evento recebido, mas ainda nao ha mensagem de texto processavel.",
                normalized=event.model_dump(),
            )

        logger.info("webhook_text", extra={"phone_tail": phone_tail})
        _send_evolution_typing(event.from_phone, thinking=True)
        state = app_graph.invoke(event.to_whatsapp_input().model_dump())
        logger.info("webhook_processed", extra={"phone_tail": phone_tail, "action": _state_action(state)})
        outbound = _send_evolution_reply(event.from_phone, state["reply"])
        delegation_outbound = _send_delegation_notification(state)
        invite_outbound = _send_invite_notification(state)
        invite_accepted_outbound = _send_invite_accepted_notification(state)
        task_update_outbound = _send_task_update_notification(state)
        return EvolutionWebhookResponse(
            status="processed",
            reply=state["reply"],
            normalized=event.model_dump(),
            parsed=state.get("parsed"),
            result=state.get("result", {})
            | {
                "delegation_outbound": delegation_outbound,
                "invite_outbound": invite_outbound,
                "invite_accepted_outbound": invite_accepted_outbound,
                "task_update_outbound": task_update_outbound,
            },
            outbound_status=outbound["status"],
            outbound_error=outbound.get("error"),
        )
    except Exception as exc:
        logger.error("webhook_unhandled_error", extra={"phone_tail": phone_tail, "exc_type": type(exc).__name__}, exc_info=True)
        _send_error_reply(event.from_phone)
        return EvolutionWebhookResponse(
            status="error",
            reply="Algo deu errado. Tente novamente em instantes.",
            normalized=event.model_dump(),
        )


@app.post("/jobs/reminders")
def run_reminders(
    payload: dict[str, Any] | None = Body(default=None),
    x_job_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not settings.reminder_job_secret and settings.app_env == "production":
        raise HTTPException(status_code=503, detail="reminder_job_secret_not_configured")
    if settings.reminder_job_secret and x_job_secret != settings.reminder_job_secret:
        raise HTTPException(status_code=401, detail="invalid_job_secret")

    state = reminder_graph.invoke(payload or {})
    return {
        "status": "processed",
        "result": state.get("result", {}),
        "outbound": [message.model_dump(mode="json") for message in state.get("outbound", [])],
    }


@app.post("/jobs/notifications")
def run_notification_retries(
    payload: dict[str, Any] | None = Body(default=None),
    x_job_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not settings.reminder_job_secret and settings.app_env == "production":
        raise HTTPException(status_code=503, detail="reminder_job_secret_not_configured")
    if settings.reminder_job_secret and x_job_secret != settings.reminder_job_secret:
        raise HTTPException(status_code=401, detail="invalid_job_secret")

    payload = payload or {}
    now = _parse_job_now(payload.get("now"))
    limit = int(payload.get("limit", 20))
    notifications = store.list_due_notifications(now=now, limit=limit)
    results = [_send_notification_record(notification) for notification in notifications]

    sent = sum(1 for item in results if item["status"] == "sent")
    permanently_failed = sum(1 for item in results if item["status"] == "failed")
    pending = sum(1 for item in results if item["status"] == "pending_retry")
    logger.info(
        "notification_job_complete",
        extra={"picked": len(notifications), "sent": sent, "pending_retry": pending, "permanently_failed": permanently_failed},
    )
    if pending:
        logger.warning("notification_retries_pending", extra={"count": pending})

    failed_last_24h = store.count_failed_notifications(
        since=now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    if failed_last_24h:
        logger.error(
            "notification_failures_today",
            extra={"count": failed_last_24h},
        )

    return {
        "status": "processed",
        "picked": len(notifications),
        "sent": sent,
        "pending_retry": pending,
        "permanently_failed": permanently_failed,
        "failed_last_24h": failed_last_24h,
        "results": results,
    }


def _send_evolution_reply(to_phone: str, reply: str) -> dict[str, str]:
    if not settings.evolution_send_enabled:
        return {"status": "dry_run"}

    client = build_evolution_client()
    if client is None:
        return {"status": "not_configured"}

    try:
        response = client.send_text(number=to_phone, text=reply)
    except Exception as exc:
        logger.error("evolution_send_failed", extra={"exc_type": type(exc).__name__}, exc_info=True)
        return {"status": "failed", "error": str(exc)}

    return {"status": "sent", "provider_response": response}


def _send_evolution_reply_with_tts(to_phone: str, reply: str) -> dict[str, str]:
    audio_bytes = generate_tts(reply)
    if audio_bytes:
        client = build_evolution_client()
        if client is not None:
            try:
                response = client.send_audio(number=to_phone, audio_bytes=audio_bytes)
                return {"status": "sent_audio", "provider_response": response}
            except Exception as exc:
                logger.warning("tts_send_failed_falling_back_to_text", extra={"exc_type": type(exc).__name__})
    return _send_evolution_reply(to_phone, reply)


def _state_action(state: dict[str, Any]) -> str | None:
    parsed = state.get("parsed")
    if parsed is None:
        return None
    return str(getattr(parsed, "action", None) or "")


def _send_evolution_typing(to_phone: str, thinking: bool = False) -> dict[str, str]:
    """Send a WhatsApp 'composing' presence indicator.

    `thinking=True` uses a longer delay (evolution_typing_thinking_delay_ms) to
    keep the animation alive while the agent is processing.  The default short
    delay is used for the quick pulse that fires just before the reply is sent.
    """
    if not settings.evolution_send_enabled:
        return {"status": "skipped", "reason": "send_disabled"}

    client = build_evolution_client()
    if client is None:
        return {"status": "not_configured"}
    delay_ms = settings.evolution_typing_thinking_delay_ms if thinking else settings.evolution_typing_delay_ms
    return _send_typing_with_client(client, to_phone, delay_ms=delay_ms)


def _send_typing_with_client(client: Any, to_phone: str, delay_ms: int | None = None) -> dict[str, str]:
    if not settings.evolution_typing_enabled or not hasattr(client, "send_presence"):
        return {"status": "skipped"}

    if delay_ms is None:
        delay_ms = settings.evolution_typing_delay_ms
    try:
        response = client.send_presence(
            number=to_phone,
            presence="composing",
            delay_ms=delay_ms,
        )
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}
    return {"status": "sent", "provider_response": response}


def _send_delegation_notification(state: dict[str, Any]) -> dict[str, str]:
    result = state.get("result", {})
    if not result.get("should_notify_assignee"):
        return {"status": "skipped"}

    assignee_phone = result.get("assignee_phone")
    if not assignee_phone:
        return {"status": "skipped", "reason": "assignee_phone_missing"}

    message = _format_delegation_notification(result)
    notification = store.enqueue_notification(
        company_id=state["context"]["company_id"],
        task_id=result["task_id"],
        recipient_user_id=result["assigned_to"],
        recipient_phone=assignee_phone,
        notification_type="task_delegated",
        message=message,
    )
    send_result = _send_notification_record(notification)
    return {
        "notification_id": notification["id"],
        "status": send_result["status"],
        "error": send_result.get("error"),
    }


def _send_invite_notification(state: dict[str, Any]) -> dict[str, str]:
    result = state.get("result", {})
    if not result.get("should_notify_invitee"):
        return {"status": "skipped"}

    message = _format_invite_notification(result)
    notification = store.enqueue_notification(
        company_id=result["company_id"],
        task_id=None,
        recipient_user_id=None,
        recipient_phone=result["phone"],
        notification_type="company_invite",
        message=message,
    )
    send_result = _send_notification_record(notification)
    return {
        "notification_id": notification["id"],
        "status": send_result["status"],
        "error": send_result.get("error"),
    }


def _send_invite_accepted_notification(state: dict[str, Any]) -> dict[str, str]:
    result = state.get("result", {})
    if not result.get("should_notify_inviter"):
        return {"status": "skipped"}

    inviter_phone = result.get("invited_by_phone")
    if not inviter_phone:
        return {"status": "skipped", "reason": "inviter_phone_missing"}

    message = _format_invite_accepted_notification(result)
    notification = store.enqueue_notification(
        company_id=result["company_id"],
        task_id=None,
        recipient_user_id=result.get("invited_by"),
        recipient_phone=inviter_phone,
        notification_type="company_invite_accepted",
        message=message,
    )
    send_result = _send_notification_record(notification)
    return {
        "notification_id": notification["id"],
        "status": send_result["status"],
        "error": send_result.get("error"),
    }

def _send_task_update_notification(state: dict[str, Any]) -> dict[str, str]:
    result = state.get("result", {})
    context = state.get("context") or {}
    company_id = context.get("company_id", "")
    update_type = result.get("task_update_type", "updated")

    creator_result: dict[str, str] | None = None
    assignee_result: dict[str, str] | None = None

    # Assignee acts → notify creator
    if result.get("should_notify_task_creator"):
        creator_phone = result.get("created_by_phone")
        if creator_phone:
            message = _format_task_update_notification(result)
            notification = store.enqueue_notification(
                company_id=company_id,
                task_id=result.get("task_id"),
                recipient_user_id=result.get("created_by"),
                recipient_phone=creator_phone,
                notification_type=f"task_{update_type}",
                message=message,
            )
            send = _send_notification_record(notification)
            creator_result = {
                "notification_id": notification["id"],
                "status": send["status"],
                "error": send.get("error"),
            }

    # Creator acts → notify assignee
    if result.get("should_notify_task_assignee"):
        assignee_phone = result.get("assignee_phone")
        if assignee_phone:
            message = _format_task_update_for_assignee(result)
            notification = store.enqueue_notification(
                company_id=company_id,
                task_id=result.get("task_id"),
                recipient_user_id=result.get("assigned_to"),
                recipient_phone=assignee_phone,
                notification_type=f"task_{update_type}_by_creator",
                message=message,
            )
            send = _send_notification_record(notification)
            assignee_result = {
                "notification_id": notification["id"],
                "status": send["status"],
                "error": send.get("error"),
            }

    if assignee_result:
        return assignee_result
    if creator_result:
        return creator_result
    return {"status": "skipped"}

def _send_notification_record(notification: dict[str, Any]) -> dict[str, str]:
    send_result = _send_evolution_reply(notification["recipient_phone"], notification["message"])
    now = datetime.now(timezone.utc)
    if send_result["status"] == "sent":
        store.mark_notification_sent(
            notification_id=notification["id"],
            sent_at=now,
            provider_response=send_result.get("provider_response"),
        )
        return {
            "notification_id": notification["id"],
            "status": "sent",
            "to_phone": notification["recipient_phone"],
        }

    error = send_result.get("error") or send_result["status"]
    permanently_failed = store.mark_notification_failed(
        notification_id=notification["id"],
        error=error,
        failed_at=now,
    )
    if permanently_failed:
        logger.error(
            "notification_permanently_failed",
            extra={
                "notification_id": notification["id"],
                "notification_type": notification.get("notification_type"),
                "exc_type": type(error).__name__ if not isinstance(error, str) else "send_error",
                "last_error": error,
            },
        )
    return {
        "notification_id": notification["id"],
        "status": "pending_retry" if not permanently_failed else "failed",
        "to_phone": notification["recipient_phone"],
        "error": error,
    }


def _format_delegation_notification(result: dict[str, Any]) -> str:
    lines = [
        "📌 Nova tarefa para você.",
        "",
        f"Tarefa: {result['title']}",
    ]
    if result.get("created_by_name"):
        lines.append(f"👤 Criada por: {result['created_by_name']}")
    if result.get("client_name"):
        lines.append(f"🏢 Cliente: {result['client_name']}")
    if result.get("due_at"):
        lines.append(f"⏰ Prazo: {_format_due_at(result['due_at'])}")
    else:
        lines.append("⏰ Prazo: sem prazo")
    lines.extend(
        [
            "",
            "Você pode responder naturalmente: concluído, comecei a fazer, preciso de mais prazo ou minhas tarefas.",
        ]
    )
    return "\n".join(lines)

def _format_invite_notification(result: dict[str, Any]) -> str:
    company_name = result.get("company_name") or "sua empresa"
    lines = [
        "🏢 Você recebeu um convite no Delega AI.",
        "",
        f"Empresa: {company_name}",
        f"👤 Convidado por: {result['invited_by_name']}",
    ]
    if result.get("job_title"):
        lines.append(f"Cargo: {result['job_title']}")
    lines.extend(
        [
            "",
            "Responda naturalmente: aceitar convite ou recusar.",
        ]
    )
    return "\n".join(lines)

def _format_invite_accepted_notification(result: dict[str, Any]) -> str:
    company_name = result.get("company_name") or "sua empresa"
    user_name = result.get("user_name") or "O colaborador"
    lines = [
        f"✅ {user_name} aceitou o convite e agora faz parte da {company_name}.",
    ]
    if result.get("job_title"):
        lines.append(f"Cargo: {result['job_title']}")
    lines.extend(
        [
            "",
            "Vocês já podem trocar delegações pelo Delega AI.",
        ]
    )
    return "\n".join(lines)

def _format_task_update_notification(result: dict[str, Any]) -> str:
    actor = result.get("actor_name") or "O colaborador"
    title = result.get("title") or "a tarefa"
    update_type = result.get("task_update_type")

    if update_type == "started":
        lines = [f"🔄 {actor} começou a tarefa.", "", f"📌 Tarefa: {title}", "Status: em andamento"]
    elif update_type == "completed":
        lines = [f"✅ {actor} concluiu a tarefa.", "", f"📌 Tarefa: {title}", "Status: concluída"]
    elif update_type == "rescheduled":
        lines = [
            f"🔁 {actor} atualizou o prazo da tarefa.",
            "",
            f"📌 Tarefa: {title}",
            f"⏰ Novo prazo: {_format_due_at(result['due_at'])}" if result.get("due_at") else "⏰ Novo prazo: sem prazo",
        ]
    elif update_type == "cannot_do":
        lines = [
            f"⚠️ {actor} sinalizou que não consegue fazer a tarefa.",
            "",
            f"📌 Tarefa: {title}",
            "A tarefa continua aberta.",
        ]
    elif update_type == "needs_help":
        lines = [
            f"⚠️ {actor} pediu ajuda com a tarefa.",
            "",
            f"📌 Tarefa: {title}",
            "A tarefa continua aberta.",
        ]
    elif update_type == "reassign_requested":
        lines = [
            f"🔁 {actor} pediu para repassar a tarefa.",
            "",
            f"📌 Tarefa: {title}",
            "A tarefa continua aberta.",
        ]
    elif update_type == "edited":
        lines = [f"✅ {actor} editou a tarefa.", "", f"📌 Tarefa: {title}"]
    elif update_type == "cancelled":
        lines = [
            f"❌ {actor} cancelou a tarefa.",
            "",
            f"📌 Tarefa: {title}",
            "Status: cancelada",
        ]
    elif update_type == "due_date_cleared":
        lines = [
            f"✅ {actor} removeu o prazo da tarefa.",
            "",
            f"📌 Tarefa: {title}",
            "⏰ Prazo: sem prazo",
        ]
    else:
        lines = [f"✅ {actor} atualizou uma tarefa.", "", f"📌 Tarefa: {title}"]

    return "\n".join(lines)


def _format_task_update_for_assignee(result: dict[str, Any]) -> str:
    actor = result.get("actor_name") or "Quem criou a tarefa"
    title = result.get("title") or "a tarefa"
    update_type = result.get("task_update_type")

    if update_type == "rescheduled":
        lines = [
            f"🔁 {actor} atualizou o prazo da sua tarefa.",
            "",
            f"📌 Tarefa: {title}",
            f"⏰ Novo prazo: {_format_due_at(result['due_at'])}" if result.get("due_at") else "⏰ Novo prazo: sem prazo",
        ]
    elif update_type == "edited":
        lines = [f"✅ {actor} editou a tarefa.", "", f"📌 Tarefa: {title}"]
    elif update_type == "completed":
        lines = [f"✅ {actor} marcou a tarefa como concluída.", "", f"📌 Tarefa: {title}"]
    elif update_type == "cancelled":
        lines = [
            f"❌ {actor} cancelou a tarefa.",
            "",
            f"📌 Tarefa: {title}",
            "Esta tarefa foi cancelada e removida da sua lista.",
        ]
    elif update_type == "due_date_cleared":
        lines = [
            f"✅ {actor} removeu o prazo da sua tarefa.",
            "",
            f"📌 Tarefa: {title}",
            "⏰ Prazo: sem prazo",
        ]
    else:
        lines = [f"✅ {actor} atualizou a tarefa.", "", f"📌 Tarefa: {title}"]

    return "\n".join(lines)
def _format_due_at(value: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    due_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if due_at.tzinfo is not None:
        due_at = due_at.astimezone(ZoneInfo("America/Sao_Paulo"))
    return due_at.strftime("%d/%m as %H:%M")


def _parse_job_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _process_contact_event(event) -> EvolutionWebhookResponse:
    _send_evolution_typing(event.from_phone, thinking=True)
    state = app_graph.invoke({
        "from_phone": event.from_phone,
        "message": f"Contato compartilhado: {event.contact_name or 'Desconhecido'}",
        "provider_message_id": event.provider_message_id,
        "contact_share": {
            "name": event.contact_name,
            "phone": event.contact_phone,
        },
    })
    outbound = _send_evolution_reply(event.from_phone, state["reply"])
    invite_outbound = _send_invite_notification(state)
    return EvolutionWebhookResponse(
        status="contact_processed",
        reply=state["reply"],
        normalized=event.model_dump(),
        parsed=state.get("parsed"),
        result=state.get("result", {}) | {"invite_outbound": invite_outbound},
        outbound_status=outbound["status"],
        outbound_error=outbound.get("error"),
    )


def _process_audio_event(event) -> EvolutionWebhookResponse:
    _send_evolution_typing(event.from_phone, thinking=True)
    glossary: list[str] = []
    try:
        context = store.identify_user(event.from_phone)
        members = store.list_company_members(context["company_id"])
        clients = store.list_clients(context["company_id"])
        glossary = build_transcription_glossary(
            team_members=members,
            clients=clients,
            company_name=context.get("company_name"),
        )
    except Exception as exc:
        logger.warning("transcription_glossary_unavailable", extra={"exc_type": type(exc).__name__})

    try:
        transcription = transcribe_evolution_audio(event, glossary=glossary)
    except AudioTranscriptionError as exc:
        reply = _audio_error_reply(str(exc))
        outbound = _send_evolution_reply(event.from_phone, reply)
        return EvolutionWebhookResponse(
            status="transcription_failed",
            reply=reply,
            normalized=event.model_dump(),
            outbound_status=outbound["status"],
            outbound_error=outbound.get("error"),
        )

    state = app_graph.invoke(
        WhatsAppInput(
            from_phone=event.from_phone,
            message=transcription,
            provider_message_id=event.provider_message_id,
        ).model_dump()
    )
    outbound = _send_evolution_reply_with_tts(event.from_phone, state["reply"])
    delegation_outbound = _send_delegation_notification(state)
    invite_outbound = _send_invite_notification(state)
    invite_accepted_outbound = _send_invite_accepted_notification(state)
    task_update_outbound = _send_task_update_notification(state)
    return EvolutionWebhookResponse(
        status="audio_processed",
        reply=state["reply"],
        normalized=event.model_dump() | {"transcription": transcription},
        parsed=state.get("parsed"),
        result=state.get("result", {})
        | {
            "transcription": transcription,
            "delegation_outbound": delegation_outbound,
            "invite_outbound": invite_outbound,
            "invite_accepted_outbound": invite_accepted_outbound,
            "task_update_outbound": task_update_outbound,
        },
        outbound_status=outbound["status"],
        outbound_error=outbound.get("error"),
    )


def _send_error_reply(to_phone: str) -> None:
    try:
        _send_evolution_reply(to_phone, "Algo deu errado. Tente novamente em instantes.")
    except Exception:
        pass


def _audio_error_reply(error: str) -> str:
    if error == "openai_api_key_not_configured":
        return "Recebi seu audio, mas a transcricao ainda nao esta configurada."
    if error == "audio_payload_has_no_media":
        return "Recebi seu audio, mas ele veio sem midia para transcrever."
    if error == "audio_file_too_large":
        return "Recebi seu audio, mas ele e grande demais para processar agora."
    if error in {"unsupported_or_corrupted_audio", "downloaded_media_is_not_audio"}:
        return (
            "Recebi seu audio, mas o arquivo chegou em um formato que nao consegui transcrever. "
            "Verifique se o Webhook Base64 esta ativado na Evolution e tente novamente."
        )
    return "Recebi seu audio, mas nao consegui transcrever desta vez."






