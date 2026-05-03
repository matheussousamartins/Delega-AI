from datetime import datetime
import re
from unicodedata import normalize
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph

from whatsapp_task_agent.extractor import extract_onboarding_value
from whatsapp_task_agent.parser import parse_due_date, parse_message
from whatsapp_task_agent.schemas import Action, AgentState, Intent, ParsedCommand, TaskStatus
from whatsapp_task_agent.settings import settings
from whatsapp_task_agent.store import store
from whatsapp_task_agent import tools

APP_TIMEZONE = ZoneInfo("America/Sao_Paulo")


def identify_context(state: AgentState) -> AgentState:
    invite = store.get_pending_invite_by_phone(state["from_phone"])
    if invite is not None:
        return {
            "invite": invite,
            "result": {},
        }

    try:
        return {"context": store.identify_user(state["from_phone"])}
    except ValueError:
        session = store.get_onboarding_session(state["from_phone"])
        if session is None:
            session = store.start_onboarding_session(state["from_phone"])
            session = {**session, "is_new": True}
        return {
            "onboarding": session,
            "result": {},
        }


def parse_intent(state: AgentState) -> AgentState:
    context = state.get("context")
    history: list[dict] = []
    team_members: list[dict] = []
    if context:
        from whatsapp_task_agent.settings import settings as _settings
        history = store.get_recent_messages(
            company_id=context["company_id"],
            user_id=context["user_id"],
            limit=_settings.conversation_history_limit,
        )
        team_members = store.list_company_members(context["company_id"])
    return {"parsed": parse_message(state["message"], context=context, history=history, team_members=team_members)}


def route_after_context(state: AgentState) -> str:
    if state.get("invite"):
        return "handle_invite_response"
    if state.get("onboarding"):
        return "handle_onboarding"
    if state.get("contact_share"):
        return "handle_contact_invite"
    if state.get("error"):
        return "format_reply"

    company_id = state["context"]["company_id"]
    user_id = state["context"]["user_id"]

    if _is_cancel_intent(state["message"]):
        store.clear_pending_choice(company_id, user_id)
        store.clear_pending_task_draft(company_id, user_id)
        store.clear_pending_invite_draft(company_id, user_id)
        return "handle_cancelled_state"

    if _is_paginate_intent(state["message"]):
        pending = store.get_pending_choice(company_id, user_id)
        if pending and pending.get("action") == "paginate_tasks":
            return "paginate_tasks"

    choice_number = _extract_choice_number(state["message"])
    if choice_number is not None:
        pending = store.get_pending_choice(company_id, user_id)
        if pending is not None:
            return "resolve_pending_choice"

    pending = store.get_pending_choice(company_id, user_id)
    if _is_pending_task_reschedule(pending) and (_is_no_due_date_intent(state["message"]) or _is_date_completion(state["message"])):
        return "complete_pending_task_reschedule"

    invite_draft = store.get_pending_invite_draft(company_id, user_id)
    if invite_draft is not None:
        return "complete_pending_invite_draft"

    draft = store.get_pending_task_draft(company_id, user_id)

    if draft is not None:
        if _is_no_due_date_intent(state["message"]) or _is_date_completion(state["message"]):
            return "complete_pending_task_draft"
        if _has_explicit_action_intent(state["message"]):
            store.clear_pending_task_draft(company_id, user_id)

    return "parse_intent"


def route_action(state: AgentState) -> str:
    action = state["parsed"].action
    if action is None:
        return "fallback"
    if action == Action.complete_task:
        # No task_reference → contextual completion (quoted body or single open task)
        if not state["parsed"].params.get("task_reference"):
            return "acknowledge_task_completion"
        return "complete_task"
    if action == Action.start_task:
        # No task_reference → contextual acknowledgement (look up open tasks, disambiguate)
        if not state["parsed"].params.get("task_reference"):
            return "acknowledge_task"
        return "start_task"
    if action in {Action.cannot_do_task, Action.needs_help_task, Action.reassign_task, Action.task_details}:
        return "handle_received_task_reply"
    if action == Action.reschedule_task:
        params = state["parsed"].params
        # "preciso de mais prazo" with no task_reference and no due_date → assignee signaling
        # need more time; let handle_received_task_reply identify the task contextually
        if not params.get("task_reference") and not params.get("due_date") and not params.get("clear_due_date"):
            return "handle_received_task_reply"
        return "reschedule_task"
    return action.value


def handle_cancelled_state(state: AgentState) -> AgentState:
    parsed = ParsedCommand(intent=Intent.other, action=None, params={}, confidence=100)
    return {
        "parsed": parsed,
        "reply": "Cancelado. Pode começar novamente quando quiser.",
    }


def execute_create_task(state: AgentState) -> AgentState:
    params = state["parsed"].params
    assignee_name = params.get("assignee_name")
    if assignee_name and store.find_user_by_name(state["context"]["company_id"], assignee_name) is None:
        suggestions = tools.find_assignee_suggestions(state["context"], assignee_name)
        if suggestions:
            store.save_pending_choice(
                company_id=state["context"]["company_id"],
                user_id=state["context"]["user_id"],
                action="confirm_create_task_assignee",
                matches=suggestions,
                params=params,
                ttl_minutes=settings.pending_choice_ttl_minutes,
            )
            return {
                "result": {
                    "needs_assignee_confirmation": True,
                    "heard_assignee_name": assignee_name,
                    "suggestions": suggestions,
                    **params,
                },
                "reply": _format_assignee_confirmation(params, assignee_name, suggestions),
            }
        return {
            "result": {"assignee_not_found": True, **params},
            "reply": _format_assignee_not_found(params, assignee_name),
        }
    result = tools.create_task(state["context"], state["parsed"].params)
    if not params.get("due_date"):
        return {"result": result, "reply": _format_created_without_due_date(result)}
    return {"result": result}


def execute_list_my_tasks(state: AgentState) -> AgentState:
    result = tools.list_my_tasks(state["context"], state["parsed"].params)
    if result.get("has_more"):
        store.save_pending_choice(
            company_id=state["context"]["company_id"],
            user_id=state["context"]["user_id"],
            action="paginate_tasks",
            matches=[],
            params={
                "filter": result.get("filter"),
                "view": result.get("view"),
                "client_name": result.get("client_name"),
                "page": result.get("page", 1) + 1,
            },
            ttl_minutes=settings.pending_choice_ttl_minutes,
        )
    return {"result": result}


def execute_paginate_tasks(state: AgentState) -> AgentState:
    context = state["context"]
    pending = store.get_pending_choice(context["company_id"], context["user_id"])
    parsed = ParsedCommand(intent=Intent.command, action=Action.list_my_tasks, params={}, confidence=95)
    if not pending or pending.get("action") != "paginate_tasks":
        return {
            "parsed": parsed,
            "result": {},
            "reply": "Não há mais tarefas para mostrar.",
        }
    params = dict(pending.get("params") or {})
    store.clear_pending_choice(context["company_id"], context["user_id"])
    result = tools.list_my_tasks(context, params)
    if result.get("has_more"):
        store.save_pending_choice(
            company_id=context["company_id"],
            user_id=context["user_id"],
            action="paginate_tasks",
            matches=[],
            params={
                "filter": result.get("filter"),
                "view": result.get("view"),
                "client_name": result.get("client_name"),
                "page": result.get("page", 1) + 1,
            },
            ttl_minutes=settings.pending_choice_ttl_minutes,
        )
    return {"parsed": parsed, "result": result}


def execute_complete_task(state: AgentState) -> AgentState:
    if not state["parsed"].params.get("task_reference"):
        return {
            "result": {"found": False, "reason": "missing_task_reference"},
            "reply": (
                "Qual tarefa voce quer concluir?\n\n"
                "Pode responder assim:\n"
                "concluir Revisar contrato da Dairy"
            ),
        }
    result = tools.complete_task(state["context"], state["parsed"].params)
    # Fallback: LLM extracted a reference from history but fuzzy match failed;
    # try the quoted notification body to identify the task directly
    if not result.get("found") and state.get("quoted_message_body"):
        open_tasks = store.list_tasks(
            company_id=state["context"]["company_id"],
            assigned_to=state["context"]["user_id"],
            status_filter="pending",
        )
        matched = _match_task_from_message(state["quoted_message_body"], open_tasks)
        if matched is not None:
            task = store.complete_task_by_id(
                state["context"]["company_id"],
                state["context"]["user_id"],
                str(matched.id),
            )
            if task is not None:
                return {
                    "result": {"found": True, **tools.task_update_payload(state["context"], task, "completed")},
                    "reply": f"Tarefa concluída.\n\nTarefa: {task.title}",
                }
    return {"result": result}

def acknowledge_task(state: AgentState) -> AgentState:
    context = state["context"]
    open_tasks = store.list_tasks(
        company_id=context["company_id"],
        assigned_to=context["user_id"],
        status_filter="pending",
    )
    candidates = [task for task in open_tasks if task.status == TaskStatus.pending]

    # If the user replied directly to a bot message (quoted), use the quoted text
    # to narrow down which specific task they are acknowledging.
    quoted = state.get("quoted_message_body")
    if quoted and candidates and len(candidates) > 1:
        matched = _match_task_from_message(quoted, candidates)
        if matched is not None:
            candidates = [matched]

    if not candidates:
        return {
            "parsed": ParsedCommand(intent=Intent.command, action=Action.start_task, params={}, confidence=95),
            "result": {"acknowledged": True, "found": False},
            "reply": (
                "Perfeito. Não encontrei nenhuma tarefa nova para marcar como em andamento.\n\n"
                "Para ver suas pendências, diga: minhas tarefas."
            ),
        }

    if len(candidates) == 1:
        task = store.start_task_by_id(context["company_id"], context["user_id"], str(candidates[0].id))
        if task is None:
            return {
                "parsed": ParsedCommand(intent=Intent.command, action=Action.start_task, params={}, confidence=95),
                "result": {"acknowledged": True, "found": False},
                "reply": "Perfeito. A tarefa já está na sua lista de pendências.",
            }
        return {
            "parsed": ParsedCommand(
                intent=Intent.command,
                action=Action.start_task,
                params={"task_reference": task.title},
                confidence=95,
            ),
            "result": {"acknowledged": True, "found": True, **tools.task_update_payload(context, task, "started")},
            "reply": f"Perfeito. Marquei como em andamento.\n\nTarefa: {task.title}",
        }

    matches = [
        {
            "id": str(task.id),
            "title": task.title,
            "due_at": task.due_at.isoformat() if task.due_at else None,
        }
        for task in sorted(candidates, key=lambda item: item.due_at or datetime.max)[:5]
    ]
    store.save_pending_choice(
        context["company_id"],
        context["user_id"],
        "start_task",
        matches,
        ttl_minutes=settings.pending_choice_ttl_minutes,
    )
    return {
        "parsed": ParsedCommand(intent=Intent.command, action=Action.start_task, params={}, confidence=90),
        "result": {"acknowledged": True, "ambiguous": True, "matches": matches},
        "reply": (
            "Perfeito. Qual tarefa você quer marcar como em andamento?\n\n"
            + _format_ambiguous_matches(matches)
            + "\n\nPode responder naturalmente: a primeira, a segunda, ou o número."
        ),
    }

def acknowledge_task_completion(state: AgentState) -> AgentState:
    """Complete a task contextually when no task_reference is given.

    Uses quoted_message_body (e.g., a forwarded notification) to identify the task,
    or falls back to the user's single open task, or asks for disambiguation.
    Covers complete_task, and any status-changing reply that quotes a notification.
    """
    context = state["context"]
    open_tasks = store.list_tasks(
        company_id=context["company_id"],
        assigned_to=context["user_id"],
        status_filter="pending",
    )
    parsed_base = ParsedCommand(intent=Intent.command, action=Action.complete_task, params={}, confidence=95)

    if not open_tasks:
        return {
            "parsed": parsed_base,
            "result": {"found": False},
            "reply": (
                "Não encontrei tarefas abertas para concluir.\n\n"
                "Para ver suas pendências, diga: minhas tarefas."
            ),
        }

    candidates = list(open_tasks)

    # Narrow down using quoted message body (bot notification reply)
    quoted = state.get("quoted_message_body")
    if quoted and len(candidates) > 1:
        matched = _match_task_from_message(quoted, candidates)
        if matched is not None:
            candidates = [matched]

    # Still ambiguous — try the user's typed message
    if len(candidates) > 1:
        matched = _match_task_from_message(state["message"], candidates)
        if matched is not None:
            candidates = [matched]

    if len(candidates) == 1:
        task = store.complete_task_by_id(context["company_id"], context["user_id"], str(candidates[0].id))
        if task is None:
            return {
                "parsed": parsed_base,
                "result": {"found": False},
                "reply": "Não consegui concluir essa tarefa. Pode tentar novamente?",
            }
        return {
            "parsed": ParsedCommand(
                intent=Intent.command,
                action=Action.complete_task,
                params={"task_reference": task.title},
                confidence=95,
            ),
            "result": {"found": True, **tools.task_update_payload(context, task, "completed")},
            "reply": f"Tarefa concluída.\n\nTarefa: {task.title}",
        }

    matches = [
        {
            "id": str(task.id),
            "title": task.title,
            "due_at": task.due_at.isoformat() if task.due_at else None,
        }
        for task in sorted(candidates, key=lambda t: t.due_at or datetime.max)[:5]
    ]
    store.save_pending_choice(
        context["company_id"],
        context["user_id"],
        "complete_task",
        matches,
        ttl_minutes=settings.pending_choice_ttl_minutes,
    )
    return {
        "parsed": ParsedCommand(intent=Intent.command, action=Action.complete_task, params={}, confidence=90),
        "result": {"found": True, "ambiguous": True, "matches": matches},
        "reply": (
            "Qual tarefa você concluiu?\n\n"
            + _format_ambiguous_matches(matches)
            + "\n\nPode responder naturalmente: a primeira, a segunda, ou o número."
        ),
    }


def execute_start_task(state: AgentState) -> AgentState:
    if not state["parsed"].params.get("task_reference"):
        return {
            "result": {"found": False, "reason": "missing_task_reference"},
            "reply": (
                "Qual tarefa voce quer marcar como em andamento?\n\n"
                "Pode responder assim:\n"
                "iniciar Revisar contrato da Dairy"
            ),
        }
    result = tools.start_task(state["context"], state["parsed"].params)
    if result.get("ambiguous"):
        pending_matches = [m for m in result.get("matches", []) if m.get("status") in ("pending", "overdue")]
        if len(pending_matches) == 1:
            task = store.start_task_by_id(
                state["context"]["company_id"],
                state["context"]["user_id"],
                pending_matches[0]["id"],
            )
            if task is not None:
                return {"result": {"found": True, **tools.task_update_payload(state["context"], task, "started")}}
    return {"result": result}


def execute_reschedule_task(state: AgentState) -> AgentState:
    params = state["parsed"].params

    if params.get("clear_due_date"):
        return {"result": tools.clear_task_due_date(state["context"], params)}

    if not params.get("task_reference") and not params.get("due_date"):
        return {
            "result": {"rescheduled": False, "reason": "missing_task_reference_and_due_date"},
            "reply": (
                "Qual tarefa voce quer reagendar e para quando?\n\n"
                "Pode responder assim:\n"
                "reagendar Revisar contrato da Dairy para amanha as 10"
            ),
        }
    if not params.get("task_reference"):
        due_at_raw = params.get("due_date")
        tasks = store.list_visible_tasks(
            company_id=state["context"]["company_id"],
            requester_user_id=state["context"]["user_id"],
            requester_role=state["context"]["role"],
            status_filter="open",
        )
        if not tasks:
            return {
                "result": {"rescheduled": False, "reason": "no_visible_tasks"},
                "reply": "Não encontrei tarefas abertas para atualizar o prazo.",
            }
        # User replied directly to a bot notification — resolve task from quoted body
        quoted = state.get("quoted_message_body")
        if quoted and len(tasks) > 1:
            matched = _match_task_from_message(quoted, tasks)
            if matched is not None:
                tasks = [matched]
        if len(tasks) == 1 and due_at_raw:
            due_at = datetime.fromisoformat(due_at_raw.replace("Z", "+00:00"))
            updated = store.reschedule_task_by_id(
                state["context"]["company_id"],
                state["context"]["user_id"],
                str(tasks[0].id),
                due_at,
            )
            if updated is None:
                return {
                    "result": {"rescheduled": False, "reason": "not_found"},
                    "reply": "Não consegui atualizar essa tarefa.",
                }
            return {
                "result": {"rescheduled": True, **tools.task_update_payload(state["context"], updated, "rescheduled")},
                "reply": (
                    "Prazo atualizado.\n\n"
                    f"Tarefa: {updated.title}\n"
                    f"Novo prazo: {_format_due_at(updated.due_at.isoformat() if updated.due_at else None)}"
                ),
            }
        if due_at_raw:
            matches = _task_matches(tasks)
            store.save_pending_choice(
                state["context"]["company_id"],
                state["context"]["user_id"],
                "reschedule_task",
                matches,
                params={"due_date": due_at_raw},
                ttl_minutes=settings.pending_choice_ttl_minutes,
            )
            return {
                "result": {"rescheduled": False, "reason": "ambiguous", "matches": matches},
                "reply": (
                    "Para qual tarefa devo colocar esse prazo?\n\n"
                    + _format_ambiguous_matches(matches)
                    + "\n\nPode responder naturalmente: a primeira, a segunda, ou o número."
                ),
            }
        return {
            "result": {"rescheduled": False, "reason": "missing_task_reference"},
            "reply": "Qual tarefa voce quer reagendar?",
        }
    if not params.get("due_date"):
        return {
            "result": {"rescheduled": False, "reason": "missing_due_date"},
            "reply": f"Para quando devo reagendar {params['task_reference']}?",
        }
    return {"result": tools.reschedule_task(state["context"], state["parsed"].params)}


def execute_cancel_task(state: AgentState) -> AgentState:
    params = state["parsed"].params
    if not params.get("task_reference"):
        return {
            "result": {"cancelled": False, "reason": "missing_task_reference"},
            "reply": (
                "Qual tarefa você quer cancelar?\n\n"
                "Diga: cancelar tarefa Nome da Tarefa"
            ),
        }
    return {"result": tools.cancel_task(state["context"], params)}


def execute_edit_task(state: AgentState) -> AgentState:
    params = state["parsed"].params
    if not params.get("task_reference") and not any(
        params.get(k) for k in ("new_title", "new_assignee_name", "new_client_name")
    ):
        return {
            "result": {"edited": False, "reason": "missing_params"},
            "reply": (
                "Para editar uma tarefa, diga o que quer mudar. Exemplos:\n\n"
                "muda o responsável da tarefa X para Leo\n"
                "muda o cliente da tarefa X para Nanocare\n"
                "muda o título da tarefa X para Novo Título"
            ),
        }
    return {"result": tools.edit_task(state["context"], params)}


def execute_task_status(state: AgentState) -> AgentState:
    return {"result": tools.task_status(state["context"], state["parsed"].params)}


def execute_team_summary(state: AgentState) -> AgentState:
    return {"result": tools.team_summary(state["context"], state["parsed"].params)}


def execute_invite_user(state: AgentState) -> AgentState:
    context = state["context"]
    params = dict(state["parsed"].params or {})
    if params.get("name") and params.get("phone") and not params.get("job_title"):
        store.save_pending_invite_draft(
            context["company_id"],
            context["user_id"],
            params,
            ttl_minutes=settings.pending_invite_ttl_hours * 60,
        )
        return {
            "result": {"invite_draft": True, "params": params},
            "reply": _format_invite_missing_job_title(params),
        }
    return {"result": tools.invite_user(context, params)}



def execute_edit_member(state: AgentState) -> AgentState:
    context = state["context"]
    if context["role"] not in ("owner", "admin", "manager"):
        return {
            "result": {"edited": False, "reason": "permission_denied"},
            "reply": "Só gestores e administradores podem renomear colaboradores.",
        }

    params = state["parsed"].params
    current_name = params.get("current_name")
    new_name = params.get("new_name")

    if not current_name or not new_name:
        return {
            "result": {"edited": False, "reason": "missing_params"},
            "reply": "Para renomear um colaborador, diga o nome atual e o novo nome. Exemplo: muda o nome do Leo para Leonardo.",
        }

    target_id = store.find_user_by_name(context["company_id"], current_name)
    if target_id is None:
        return {
            "result": {"edited": False, "reason": "member_not_found", "current_name": current_name},
            "reply": f"Não encontrei {current_name} no time.",
        }

    updated = store.update_member_name(context["company_id"], str(target_id), new_name)
    if not updated:
        return {
            "result": {"edited": False, "reason": "update_failed"},
            "reply": "Não consegui atualizar o nome. Tente novamente.",
        }

    return {
        "result": {"edited": True, "old_name": current_name, "new_name": new_name},
        "reply": f"Nome atualizado.\n\n{current_name} → {new_name}",
    }


def complete_pending_invite_draft(state: AgentState) -> AgentState:
    context = state["context"]
    draft = store.get_pending_invite_draft(context["company_id"], context["user_id"])
    parsed = ParsedCommand(intent=Intent.command, action=Action.invite_user, params={}, confidence=90)
    if draft is None:
        return {
            "parsed": parsed,
            "reply": "Não encontrei um convite em andamento. Para convidar alguém, compartilhe o contato aqui na conversa.",
        }

    job_title = _extract_invite_job_title(state["message"])
    if not job_title:
        return {
            "parsed": parsed,
            "reply": "Qual será o cargo ou função dessa pessoa no time? Exemplo: Desenvolvedor, Gestor Comercial, CEO.",
        }

    params = dict(draft["params"])
    params["job_title"] = job_title
    store.clear_pending_invite_draft(context["company_id"], context["user_id"])
    result = tools.invite_user(context, params)
    return {"parsed": parsed, "result": result}
def complete_pending_task_draft(state: AgentState) -> AgentState:
    context = state["context"]
    draft = store.get_pending_task_draft(context["company_id"], context["user_id"])
    no_due_date = _is_no_due_date_intent(state["message"])
    due_date = None if no_due_date else parse_due_date(state["message"])
    parsed = ParsedCommand(intent=Intent.task, action=Action.create_task, params={}, confidence=80)

    if draft is None or (due_date is None and not no_due_date):
        return {
            "parsed": parsed,
            "result": {},
            "reply": "Me envie o prazo da tarefa. Exemplo: amanha as 18. Se preferir, responda: sem prazo.",
        }

    params = dict(draft["params"])
    params["due_date"] = due_date
    parsed = ParsedCommand(intent=Intent.task, action=Action.create_task, params=params, confidence=85)
    store.clear_pending_task_draft(context["company_id"], context["user_id"])
    return {
        "parsed": parsed,
        "result": tools.create_task(context, params),
    }


def handle_invite_response(state: AgentState) -> AgentState:
    invite = state["invite"]
    message = _clean_onboarding_answer(state["message"])
    parsed = ParsedCommand(intent=Intent.other, action=None, params={}, confidence=100)

    if _is_invite_decline(message):
        store.decline_invite(invite["id"])
        store.clear_onboarding_session(state["from_phone"])
        return {
            "parsed": parsed,
            "reply": "Convite recusado. Se mudar de ideia, peça para o responsável enviar um novo convite.",
        }

    if _is_invite_acceptance(message):
        context = store.accept_invite(invite["id"], state["from_phone"])
        if context is None:
            return {
                "parsed": parsed,
                "reply": "Esse convite não está mais disponível. Peça para o responsável reenviar o convite.",
            }
        return {
            "context": {
                "company_id": context["company_id"],
                "company_name": context["company_name"],
                "user_id": context["user_id"],
                "user_name": context["user_name"],
                "role": context["role"],
            },
            "parsed": parsed,
            "result": {
                "invite_accepted": True,
                "should_notify_inviter": True,
                "invited_by": invite.get("invited_by"),
                "invited_by_name": invite.get("invited_by_name"),
                "invited_by_phone": store.get_user_phone(invite["invited_by"]) if invite.get("invited_by") else None,
                **context,
            },
            "reply": _format_invite_accepted(context),
        }

    if _is_greeting(message):
        return {
            "parsed": parsed,
            "reply": _format_pending_invite(invite),
        }

    return {
        "parsed": parsed,
        "reply": _format_invite_response_help(invite),
    }

def handle_onboarding(state: AgentState) -> AgentState:
    session = state["onboarding"]
    raw_message = _clean_onboarding_answer(state["message"])
    parsed = ParsedCommand(intent=Intent.other, action=None, params={}, confidence=100)

    if session.get("is_new"):
        return {
            "parsed": parsed,
            "reply": (
                "Bem-vindo ao Delega AI.\n\n"
                "Você pode delegar tarefas por texto ou áudio. "
                "Vou configurar seu espaço de trabalho rapidamente.\n\n"
                "Qual é o nome da sua empresa?"
            ),
        }

    if session["step"] == "company_name" and not session.get("company_name"):
        value = extract_onboarding_value("company_name", raw_message) or raw_message
        if not _is_valid_onboarding_value(value):
            return {
                "parsed": parsed,
                "reply": "Qual é o nome da sua empresa? Pode falar ou digitar.",
            }
        store.update_onboarding_session(
            state["from_phone"],
            company_name=value,
            step="user_name",
        )
        return {
            "parsed": parsed,
            "reply": (
                f"Perfeito. Espaço criado para {value}.\n\n"
                "Agora me diga seu nome completo."
            ),
        }

    if session["step"] == "user_name":
        value = extract_onboarding_value("user_name", raw_message) or raw_message
        if not _is_valid_onboarding_value(value):
            return {
                "parsed": parsed,
                "reply": "Qual é o seu nome completo?",
            }
        store.update_onboarding_session(
            state["from_phone"],
            user_name=value,
            step="job_title",
        )
        return {
            "parsed": parsed,
            "reply": (
                f"Prazer, {value}.\n\n"
                "Qual é o seu cargo? Exemplo: CEO, Gerente Comercial, Desenvolvedor."
            ),
        }

    if session["step"] == "job_title":
        value = extract_onboarding_value("job_title", raw_message) or raw_message
        if not _is_valid_onboarding_value(value):
            return {
                "parsed": parsed,
                "reply": "Qual é o seu cargo? Exemplo: CEO, Gerente, Desenvolvedor.",
            }
        store.update_onboarding_session(
            state["from_phone"],
            job_title=value,
            step="completed",
        )
        context = store.complete_onboarding_session(state["from_phone"])
        return {
            "context": {
                "company_id": context["company_id"],
                "company_name": context["company_name"],
                "user_id": context["user_id"],
                "user_name": context["user_name"],
                "role": "owner",
            },
            "parsed": parsed,
            "result": {"onboarding_completed": True, **context},
            "reply": (
                f"Tudo pronto, {context['user_name']}.\n\n"
                f"Empresa: {context['company_name']}\n"
                f"Perfil: {context['job_title']}\n\n"
                "Agora você pode delegar tarefas por texto ou áudio. "
                "Pode falar naturalmente; eu organizo responsável, tarefa e prazo.\n\n"
                "Para convidar alguém, compartilhe o contato aqui na conversa ou diga: quero convidar uma pessoa.\n"
                "Exemplo: Leo, ajustar o relatório comercial até amanhã às 10h."
            ),
        }

    return {
        "parsed": parsed,
        "reply": "Vamos continuar seu cadastro. Me envie a proxima informacao solicitada.",
    }


def handle_contact_invite(state: AgentState) -> AgentState:
    contact = state.get("contact_share") or {}
    context = state.get("context")
    parsed = ParsedCommand(intent=Intent.command, action=Action.invite_user, params={}, confidence=100)

    if not context:
        return {
            "parsed": parsed,
            "reply": "Complete seu cadastro primeiro para poder convidar colaboradores.",
        }

    name = contact.get("name")
    phone = contact.get("phone")

    if not phone:
        return {
            "parsed": parsed,
            "reply": (
                "Recebi o contato, mas nao consegui identificar o numero.\n\n"
                "Tente compartilhar novamente ou envie: convidar Nome 5511999990001"
            ),
        }

    params = {
        "name": name,
        "phone": phone,
        "job_title": None,
        "role": "member",
    }
    store.save_pending_invite_draft(
        context["company_id"],
        context["user_id"],
        params,
        ttl_minutes=settings.pending_invite_ttl_hours * 60,
    )
    return {
        "parsed": parsed,
        "result": {"invite_draft": True, "params": params},
        "reply": _format_invite_missing_job_title(params),
    }


def execute_pending_choice(state: AgentState) -> AgentState:
    choice_number = _extract_choice_number(state["message"])
    if choice_number is None:
        return {"reply": "Pode responder com o número ou de forma natural, como: a primeira."}

    context = state["context"]
    pending = store.get_pending_choice(context["company_id"], context["user_id"])
    if pending is not None:
        matches = pending.get("matches", [])
        if choice_number < 1 or choice_number > len(matches):
            return {"reply": f"Escolha uma opção entre 1 e {len(matches)}."}
        selected = matches[choice_number - 1]
        action = pending.get("action")

        if action == "show_task_details":
            store.clear_pending_choice(context["company_id"], context["user_id"])
            return {"result": {"resolved": True, "action": action, "task_id": selected["id"]}, "reply": _format_received_task_details(selected)}

        if action == "cannot_do_task":
            store.clear_pending_choice(context["company_id"], context["user_id"])
            return {"result": {"resolved": True, "action": action, **_task_update_from_match(context, selected, "cannot_do")}, "reply": _format_cannot_do_response(selected)}

        if action in {"needs_help_task", "reassign_task"}:
            update_type = "needs_help" if action == "needs_help_task" else "reassign_requested"
            store.clear_pending_choice(context["company_id"], context["user_id"])
            return {
                "result": {"resolved": True, "action": action, **_task_update_from_match(context, selected, update_type)},
                "reply": _format_negative_task_response(selected, update_type),
            }

        if action == "select_task_for_reschedule":
            store.save_pending_choice(
                context["company_id"],
                context["user_id"],
                "reschedule_task",
                [selected],
                params={"awaiting_due_date": True, "selected_task_id": selected["id"]},
                ttl_minutes=settings.pending_choice_ttl_minutes,
            )
            return {
                "result": {"resolved": True, "action": action, "task_id": selected["id"]},
                "reply": (
                    "Claro. Qual novo prazo devo colocar?\n\n"
                    f"Tarefa: {selected['title']}\n"
                    "Exemplos: amanhã às 18, sexta às 10, 04/05 às 15."
                ),
            }

    result = tools.resolve_pending_choice(state["context"], choice_number)
    if result.get("resolved") and result.get("action") == "cancel_task":
        return {"result": result, "reply": f"Tarefa cancelada.\n\nTarefa: {result['title']}"}
    if result.get("resolved") and result.get("action") == "clear_task_due_date":
        return {"result": result, "reply": f"Prazo removido.\n\nTarefa: {result['title']}\nPrazo: sem prazo"}
    if result.get("resolved") and result.get("action") == "edit_task":
        return {"result": result, "reply": _format_edited_task(result)}
    if result.get("resolved") and result.get("action") == Action.complete_task:
        return {"result": result, "reply": f"✅ Tarefa concluida\n\n📌 {result['title']}"}
    if result.get("resolved") and result.get("action") == "start_task":
        return {"result": result, "reply": f"🔄 Em andamento\n\n📌 {result['title']}"}
    if result.get("resolved") and result.get("action") == Action.reschedule_task:
        return {
            "result": result,
            "reply": (
                "Tarefa reagendada.\n\n"
                f"Tarefa: {result['title']}\n"
                f"Novo prazo: {_format_due_at(result.get('due_at'))}"
            ),
        }
    if result.get("resolved") and result.get("action") == "confirm_create_task_assignee":
        return {"result": result, "reply": _format_created_task(result)}
    if result.get("reason") == "permission_denied":
        return {"result": result, "reply": "Só quem criou a tarefa pode realizar essa ação."}
    if result.get("reason") == "invalid_choice":
        return {
            "result": result,
            "reply": f"Escolha uma opcao entre 1 e {result.get('available_count', 1)}.",
        }
    if result.get("reason") == "missing_due_date":
        return {"result": result, "reply": "Preciso da nova data para reagendar essa tarefa."}
    return {"result": result, "reply": "Essa escolha expirou. Pode me dizer de novo qual tarefa voce quer alterar?"}

_LLM_ACTION_TO_RECEIVED_INTENT: dict = {
    Action.cannot_do_task: "cannot_do",
    Action.needs_help_task: "needs_help",
    Action.reassign_task: "reassign_requested",
    Action.task_details: "details",
}


def handle_received_task_reply(state: AgentState) -> AgentState:
    context = state["context"]
    # Prefer LLM-classified action; fall back to deterministic patterns for edge cases
    parsed_action = state.get("parsed") and state["parsed"].action
    intent = _LLM_ACTION_TO_RECEIVED_INTENT.get(parsed_action) or _detect_received_task_intent(state["message"])
    tasks = _open_tasks_for_user(context)
    parsed = ParsedCommand(intent=Intent.command, action=None, params={}, confidence=95)

    if not intent:
        return {"parsed": parsed, "reply": _format_contextual_fallback(state["message"])}

    if not tasks:
        return {
            "parsed": parsed,
            "result": {"received_task_reply": intent, "found": False},
            "reply": (
                "Entendi. Não encontrei tarefas abertas na sua lista agora.\n\n"
                "Para conferir, diga: minhas tarefas."
            ),
        }

    # Use quoted message to identify the specific task when user replied directly to a notification
    quoted = state.get("quoted_message_body")
    matched = _match_task_from_message(quoted or state["message"], tasks)
    if matched is None and quoted:
        matched = _match_task_from_message(state["message"], tasks)
    if matched is not None:
        tasks = [matched]

    if intent == "more_time":
        due_date = parse_due_date(state["message"])
        if len(tasks) == 1:
            task = tasks[0]
            if due_date:
                due_at = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
                updated = store.reschedule_task_by_id(context["company_id"], context["user_id"], str(task.id), due_at)
                if updated is None:
                    return {"parsed": parsed, "reply": "Não consegui atualizar essa tarefa. Pode tentar novamente?"}
                return {
                    "parsed": ParsedCommand(
                        intent=Intent.command,
                        action=Action.reschedule_task,
                        params={"task_reference": updated.title, "due_date": due_date},
                        confidence=95,
                    ),
                    "result": {"received_task_reply": intent, "rescheduled": True, **tools.task_update_payload(context, updated, "rescheduled")},
                    "reply": (
                        "Prazo atualizado.\n\n"
                        f"Tarefa: {updated.title}\n"
                        f"Novo prazo: {_format_due_at(updated.due_at.isoformat() if updated.due_at else None)}"
                    ),
                }

            _save_pending_reschedule(context, task)
            return {
                "parsed": parsed,
                "result": {"received_task_reply": intent, "needs_due_date": True, "task_id": str(task.id)},
                "reply": (
                    "Claro. Qual novo prazo devo colocar?\n\n"
                    f"Tarefa: {task.title}\n"
                    "Exemplos: amanhã às 18, sexta às 10, 04/05 às 15."
                ),
            }

        matches = _task_matches(tasks)
        action = "reschedule_task" if due_date else "select_task_for_reschedule"
        params = {"due_date": due_date} if due_date else {}
        store.save_pending_choice(
            context["company_id"],
            context["user_id"],
            action,
            matches,
            params=params,
            ttl_minutes=settings.pending_choice_ttl_minutes,
        )
        return {
            "parsed": parsed,
            "result": {"received_task_reply": intent, "ambiguous": True, "matches": matches},
            "reply": (
                "Para qual tarefa você precisa de mais prazo?\n\n"
                + _format_ambiguous_matches(matches)
                + "\n\nPode responder naturalmente: a primeira, a segunda, ou o número."
            ),
        }

    if intent == "details":
        if len(tasks) == 1:
            return {
                "parsed": parsed,
                "result": {"received_task_reply": intent, "found": True, "task_id": str(tasks[0].id)},
                "reply": _format_received_task_details(tasks[0]),
            }
        matches = _task_matches(tasks)
        store.save_pending_choice(
            context["company_id"],
            context["user_id"],
            "show_task_details",
            matches,
            ttl_minutes=settings.pending_choice_ttl_minutes,
        )
        return {
            "parsed": parsed,
            "result": {"received_task_reply": intent, "ambiguous": True, "matches": matches},
            "reply": (
                "Qual tarefa você quer ver melhor?\n\n"
                + _format_ambiguous_matches(matches)
                + "\n\nPode responder naturalmente: a primeira, a segunda, ou o número."
            ),
        }

    if intent == "cannot_do":
        if len(tasks) == 1:
            return {
                "parsed": parsed,
                "result": {"received_task_reply": intent, "found": True, **tools.task_update_payload(context, tasks[0], "cannot_do")},
                "reply": _format_cannot_do_response(tasks[0]),
            }
        matches = _task_matches(tasks)
        store.save_pending_choice(
            context["company_id"],
            context["user_id"],
            "cannot_do_task",
            matches,
            ttl_minutes=settings.pending_choice_ttl_minutes,
        )
        return {
            "parsed": parsed,
            "result": {"received_task_reply": intent, "ambiguous": True, "matches": matches},
            "reply": (
                "Qual tarefa você não vai conseguir fazer?\n\n"
                + _format_ambiguous_matches(matches)
                + "\n\nPode responder naturalmente: a primeira, a segunda, ou o número."
            ),
        }

    if intent in {"needs_help", "reassign_requested"}:
        update_type = intent
        if len(tasks) == 1:
            return {
                "parsed": parsed,
                "result": {"received_task_reply": intent, "found": True, **tools.task_update_payload(context, tasks[0], update_type)},
                "reply": _format_negative_task_response(tasks[0], update_type),
            }
        matches = _task_matches(tasks)
        action = "needs_help_task" if intent == "needs_help" else "reassign_task"
        store.save_pending_choice(
            context["company_id"],
            context["user_id"],
            action,
            matches,
            ttl_minutes=settings.pending_choice_ttl_minutes,
        )
        prompt = "Qual tarefa precisa de ajuda?" if intent == "needs_help" else "Qual tarefa você quer repassar?"
        return {
            "parsed": parsed,
            "result": {"received_task_reply": intent, "ambiguous": True, "matches": matches},
            "reply": (
                prompt
                + "\n\n"
                + _format_ambiguous_matches(matches)
                + "\n\nPode responder naturalmente: a primeira, a segunda, ou o número."
            ),
        }

    return {"parsed": parsed, "reply": _format_contextual_fallback(state["message"])}


def complete_pending_task_reschedule(state: AgentState) -> AgentState:
    context = state["context"]
    pending = store.get_pending_choice(context["company_id"], context["user_id"])
    parsed = ParsedCommand(intent=Intent.command, action=Action.reschedule_task, params={}, confidence=95)
    if not _is_pending_task_reschedule(pending):
        return {"parsed": parsed, "reply": "Essa alteração expirou. Pode me dizer novamente qual tarefa quer reagendar?"}

    if _is_no_due_date_intent(state["message"]):
        store.clear_pending_choice(context["company_id"], context["user_id"])
        return {
            "parsed": parsed,
            "result": {"rescheduled": False, "reason": "no_due_date"},
            "reply": "Tudo bem. Mantive o prazo atual da tarefa.",
        }

    due_date = parse_due_date(state["message"])
    if not due_date:
        return {
            "parsed": parsed,
            "result": {"rescheduled": False, "reason": "missing_due_date"},
            "reply": "Qual novo prazo devo colocar? Exemplos: amanhã às 18, sexta às 10, 04/05 às 15.",
        }

    params = pending.get("params") or {}
    task_id = params.get("selected_task_id")
    if not task_id:
        matches = pending.get("matches") or []
        task_id = matches[0].get("id") if matches else None
    if not task_id:
        store.clear_pending_choice(context["company_id"], context["user_id"])
        return {"parsed": parsed, "reply": "Essa alteração expirou. Pode me dizer novamente qual tarefa quer reagendar?"}

    due_at = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
    task = store.reschedule_task_by_id(context["company_id"], context["user_id"], task_id, due_at)
    store.clear_pending_choice(context["company_id"], context["user_id"])
    if task is None:
        return {"parsed": parsed, "reply": "Não encontrei essa tarefa entre as suas tarefas visíveis."}
    return {
        "parsed": ParsedCommand(
            intent=Intent.command,
            action=Action.reschedule_task,
            params={"task_reference": task.title, "due_date": due_date},
            confidence=95,
        ),
        "result": {"rescheduled": True, **tools.task_update_payload(context, task, "rescheduled")},
        "reply": (
            "Prazo atualizado.\n\n"
            f"Tarefa: {task.title}\n"
            f"Novo prazo: {_format_due_at(task.due_at.isoformat() if task.due_at else None)}"
        ),
    }

def fallback(state: AgentState) -> AgentState:
    return {
        "result": {"fallback_reason": "unclear_message"},
        "reply": _format_contextual_fallback(state["message"]),
    }


def format_reply(state: AgentState) -> AgentState:
    if state.get("reply"):
        return {}

    action = state["parsed"].action
    result = state.get("result", {})

    if action == Action.create_task:
        return {"reply": _format_created_task(result)}

    if action == Action.list_my_tasks:
        return {"reply": _format_my_tasks_reply(result, state["parsed"].params)}

    if action == Action.complete_task:
        if not result.get("found"):
            return {"reply": "Não encontrei essa tarefa entre as suas tarefas visíveis."}
        if result.get("ambiguous"):
            lines = _format_ambiguous_matches(result.get("matches", []))
            return {
                "reply": (
                    "Encontrei mais de uma tarefa parecida.\n\n"
                    + lines
                    + "\n\nResponda com o numero da tarefa que voce quer concluir."
                )
            }
        return {"reply": f"Tarefa concluída.\n\nTarefa: {result['title']}"}

    if action == Action.start_task:
        if not result.get("found"):
            return {"reply": "Não encontrei essa tarefa entre as suas tarefas visíveis."}
        if result.get("ambiguous"):
            lines = _format_ambiguous_matches(result.get("matches", []))
            return {
                "reply": (
                    "Encontrei mais de uma tarefa parecida.\n\n"
                    + lines
                    + "\n\nResponda com o numero da tarefa que voce quer marcar como em andamento."
                )
            }
        return {"reply": f"Tarefa marcada como em andamento.\n\nTarefa: {result['title']}"}

    if action == Action.cancel_task:
        if result.get("reason") == "permission_denied":
            return {"reply": "Só quem criou a tarefa pode cancelá-la."}
        if result.get("reason") == "ambiguous":
            lines = _format_ambiguous_matches(result.get("matches", []))
            return {
                "reply": (
                    "Encontrei mais de uma tarefa parecida.\n\n"
                    + lines
                    + "\n\nQual delas você quer cancelar?"
                )
            }
        if not result.get("cancelled"):
            return {"reply": "Não encontrei essa tarefa entre as suas tarefas visíveis."}
        return {"reply": f"Tarefa cancelada.\n\nTarefa: {result['title']}"}

    if action == Action.reschedule_task and result.get("task_update_type") == "due_date_cleared":
        if result.get("reason") == "permission_denied":
            return {"reply": "Só quem criou a tarefa pode remover o prazo."}
        if result.get("reason") == "ambiguous":
            lines = _format_ambiguous_matches(result.get("matches", []))
            return {
                "reply": (
                    "Encontrei mais de uma tarefa parecida.\n\n"
                    + lines
                    + "\n\nDe qual você quer remover o prazo?"
                )
            }
        if not result.get("cleared"):
            return {"reply": "Não encontrei essa tarefa entre as suas tarefas visíveis."}
        return {"reply": f"Prazo removido.\n\nTarefa: {result['title']}\nPrazo: sem prazo"}

    if action == Action.edit_task:
        if result.get("reason") == "ambiguous":
            lines = _format_ambiguous_matches(result.get("matches", []))
            return {
                "reply": (
                    "Encontrei mais de uma tarefa parecida.\n\n"
                    + lines
                    + "\n\nResponda com o numero da tarefa que voce quer editar."
                )
            }
        if not result.get("edited"):
            return {"reply": "Não encontrei essa tarefa entre as suas tarefas visíveis."}
        return {"reply": _format_edited_task(result)}

    if action == Action.reschedule_task:
        if result.get("reason") == "ambiguous":
            lines = _format_ambiguous_matches(result.get("matches", []))
            return {
                "reply": (
                    "Encontrei mais de uma tarefa parecida.\n\n"
                    + lines
                    + "\n\nResponda com o numero da tarefa que voce quer reagendar."
                )
            }
        if not result.get("rescheduled"):
            return {"reply": "Não consegui reagendar. Preciso identificar a tarefa e a nova data."}
        return {
            "reply": (
                "Tarefa reagendada.\n\n"
                f"Tarefa: {result['title']}\n"
                f"Novo prazo: {_format_due_at(result.get('due_at'))}"
            )
        }

    if action == Action.task_status:
        return {"reply": _format_task_status_query(result)}

    if action == Action.team_summary:
        return {"reply": _format_team_summary(result)}

    if action == Action.invite_user:
        return {"reply": _format_invite_user(result)}

    return {"reply": "Tudo certo."}


def _format_ambiguous_matches(matches: list[dict]) -> str:
    if not matches:
        return "Descreva melhor a tarefa para eu identificar com seguranca."
    lines = []
    for index, match in enumerate(matches, start=1):
        due_at = f" - ⏰ vence {_format_due_at(match['due_at'])}" if match.get("due_at") else ""
        lines.append(f"{index}. 📌 {match['title']}{due_at}")
    return "\n".join(lines)


def _format_created_task(result: dict) -> str:
    if result.get("assigned_to_self"):
        return _format_created_self_task(result)

    assignee = result.get("assignee_name") or "responsável"
    lines = [
        f"Tarefa criada e enviada para {assignee}.",
        "",
        f"Tarefa: {result['title']}",
    ]
    if result.get("assignee_name"):
        lines.append(f"Responsável: {result['assignee_name']}")
    if result.get("client_name"):
        lines.append(f"Cliente: {result['client_name']}")
    lines.append(f"Prazo: {_format_due_at(result.get('due_at'))}")
    lines.append("Status: enviada")
    lines.extend(["", "Vou acompanhar e te aviso quando houver atualização."])
    return "\n".join(lines)

def _format_created_self_task(result: dict) -> str:
    lines = [
        "Tarefa criada para você.",
        "",
        f"Tarefa: {result['title']}",
    ]
    if result.get("client_name"):
        lines.append(f"Cliente: {result['client_name']}")
    lines.append(f"Prazo: {_format_due_at(result.get('due_at'))}")
    lines.append("Status: pendente")
    lines.extend(["", "Fica na sua lista de pendências."])
    return "\n".join(lines)


def _format_created_without_due_date(result: dict) -> str:
    return (
        _format_created_task(result)
        + "\n\n"
        + "Criei sem prazo. Se quiser definir depois, diga algo como: colocar prazo amanhã às 10."
    )


def _format_my_tasks_reply(result: dict, params: dict) -> str:
    view = result.get("view", "my")
    tasks_list = result.get("tasks", [])
    delegated_list = result.get("delegated_tasks", [])
    page = result.get("page", 1)
    task_filter = result.get("filter", "pending")
    client_name = result.get("client_name")
    is_done = task_filter == "done"

    if view == "delegated":
        if not delegated_list:
            return _format_empty_task_list(result, params)
        offset = (page - 1) * 10
        if is_done:
            header = f"Tarefas delegadas concluídas — {client_name}:" if client_name else "Tarefas delegadas concluídas:"
        else:
            header = f"Tarefas que você delegou — {client_name}:" if client_name else "Tarefas que você delegou:"
        lines = [f"{offset + i}. {_format_task_line(t)}" for i, t in enumerate(delegated_list, start=1)]
        body = f"{header}\n\n" + "\n".join(lines)
        if result.get("has_more"):
            body += "\n\nPara ver mais, responda: próximas tarefas"
        return body

    if view == "all":
        if not tasks_list and not delegated_list:
            return _format_empty_task_list(result, params)
        sections = []
        offset = (page - 1) * 10
        if tasks_list:
            if is_done:
                my_header = f"Suas tarefas concluídas — {client_name}:" if client_name else "Suas tarefas concluídas:"
            else:
                my_header = f"Suas tarefas abertas — {client_name}:" if client_name else "Suas tarefas abertas:"
            my_lines = [f"{offset + i}. {_format_task_line(t)}" for i, t in enumerate(tasks_list, start=1)]
            sections.append(f"{my_header}\n\n" + "\n".join(my_lines))
        if delegated_list:
            if is_done:
                del_header = f"Tarefas delegadas concluídas — {client_name}:" if client_name else "Tarefas delegadas concluídas:"
            else:
                del_header = f"Tarefas que você delegou — {client_name}:" if client_name else "Tarefas que você delegou:"
            del_lines = [f"{offset + i}. {_format_task_line(t)}" for i, t in enumerate(delegated_list, start=1)]
            sections.append(f"{del_header}\n\n" + "\n".join(del_lines))
        body = "\n\n".join(sections)
        if result.get("has_more"):
            body += "\n\nPara ver mais, responda: próximas tarefas"
        return body

    if not tasks_list:
        return _format_empty_task_list(result, params)

    offset = (page - 1) * 10
    if is_done:
        header = f"Suas tarefas concluídas — {client_name}:" if client_name else "Suas tarefas concluídas:"
    else:
        header = f"Suas tarefas abertas — {client_name}:" if client_name else "Suas tarefas abertas:"
    lines = [f"{offset + i}. {_format_task_line(t)}" for i, t in enumerate(tasks_list, start=1)]
    body = f"{header}\n\n" + "\n".join(lines)
    if result.get("has_more"):
        body += "\n\nPara ver mais, responda: próximas tarefas"
    return body


def _format_edited_task(result: dict) -> str:
    lines = ["Tarefa atualizada.", "", f"Tarefa: {result['title']}"]
    if result.get("assignee_name"):
        lines.append(f"Responsável: {result['assignee_name']}")
    return "\n".join(lines)


def _format_empty_task_list(result: dict, params: dict) -> str:
    task_filter = result.get("filter") or params.get("filter", "pending")
    client_name = result.get("client_name")
    client_suffix = f" para {client_name}" if client_name else ""
    if task_filter == "done":
        return f"Você ainda não concluiu nenhuma tarefa{client_suffix}."
    if task_filter == "overdue":
        return "Você não tem tarefas atrasadas agora."
    if task_filter == "today":
        return "Você não tem tarefas para hoje."
    if task_filter == "date":
        return "Você não tem tarefas para essa data."
    if task_filter == "all":
        return f"Você ainda não tem tarefas{client_suffix}."
    return f"Você não tem tarefas pendentes agora{client_suffix}."


def _format_task_status_query(result: dict) -> str:
    tasks = result.get("tasks", [])
    query = result.get("query", {})

    if result.get("reason") == "member_not_found":
        return f"Não encontrei {result.get('member_name', 'essa pessoa')} no time."

    if not tasks:
        if query.get("member_name"):
            return f"Não encontrei tarefas abertas para {query['member_name']} nesse contexto."
        if query.get("client_name"):
            return f"Não encontrei tarefas abertas para {query['client_name']} nesse contexto."
        if query.get("task_reference"):
            return f"Não encontrei tarefa visível relacionada a {query['task_reference']}."
        return "Não encontrei tarefas abertas nesse contexto."

    if len(tasks) == 1:
        task = tasks[0]
        lines = ["Status da tarefa", "", f"Tarefa: {task['title']}"]
        if task.get("assignee_name"):
            lines.append(f"Responsável: {task['assignee_name']}")
        if task.get("created_by_name"):
            lines.append(f"Criada por: {task['created_by_name']}")
        if task.get("client_name"):
            lines.append(f"Cliente: {task['client_name']}")
        lines.append(f"Prazo: {_format_due_at(task.get('due_at'))}")
        lines.append(f"Status: {_format_task_status(task.get('status'))}")
        if task.get("completed_at"):
            lines.append(f"Concluída em: {_format_due_at(task.get('completed_at'))}")
        return "\n".join(lines)

    title = "Tarefas encontradas"
    if query.get("member_name"):
        title = f"Tarefas de {query['member_name']}"
    elif query.get("client_name"):
        title = f"Tarefas da {query['client_name']}"

    lines = [f"{index}. {_format_task_line(task)}" for index, task in enumerate(tasks, start=1)]
    if result.get("total", len(tasks)) > len(tasks):
        lines.append(f"...e mais {result['total'] - len(tasks)} tarefa(s).")
    return title + ":\n\n" + "\n".join(lines)


def _format_team_summary(result: dict) -> str:
    view = result.get("view", "summary")
    if view == "team_tasks":
        return _format_team_task_section(
            "Tarefas do time",
            result.get("pending_tasks", []),
            empty="Sem tarefas pendentes no time.",
        )
    if view == "team_overdue":
        return _format_team_task_section(
            "Tarefas atrasadas do time",
            result.get("overdue_tasks", []),
            empty="Sem tarefas atrasadas no time.",
        )
    if view == "team_done_today":
        return _format_team_task_section(
            "Concluídas hoje",
            result.get("done_today_tasks", []),
            empty="Nenhuma tarefa concluida hoje.",
        )
    if view == "member_tasks":
        member_name = result.get("member_name") or "colaborador"
        return _format_team_task_section(
            f"Tarefas de {member_name}",
            result.get("pending_tasks", []),
            empty=f"{member_name} nao tem tarefas pendentes agora.",
        )

    lines = [
        "Resumo do time",
        "",
        f"Pendentes: {result.get('pending', 0)}",
        f"Concluídas: {result.get('done', 0)}",
        f"Atrasadas: {result.get('overdue', 0)}",
    ]
    if result.get("overdue_tasks"):
        lines.extend(["", "Atrasadas"])
        lines.extend(_format_team_task_lines(result["overdue_tasks"][:3]))
    if result.get("pending_tasks"):
        lines.extend(["", "Próximas pendentes"])
        lines.extend(_format_team_task_lines(result["pending_tasks"][:3]))
    if result.get("done_today_tasks"):
        lines.extend(["", "Concluídas hoje"])
        lines.extend(_format_team_task_lines(result["done_today_tasks"][:3], use_completed_at=True))
    lines.extend(["", "Para detalhar, diga: tarefas do time, atrasadas do time ou concluídas hoje."])
    return "\n".join(lines)


def _format_team_task_section(title: str, tasks: list[dict], empty: str) -> str:
    if not tasks:
        return f"{title}\n\n{empty}"
    return title + "\n\n" + "\n".join(_format_team_task_lines(tasks))


def _format_team_task_lines(tasks: list[dict], use_completed_at: bool = False) -> list[str]:
    lines = []
    for index, task in enumerate(tasks, start=1):
        assignee = task.get("assignee_name") or "Sem responsável"
        client = f" [{task['client_name']}]" if task.get("client_name") else ""
        timestamp = task.get("completed_at") if use_completed_at else task.get("due_at")
        label = "concluída" if use_completed_at else "prazo"
        when = f" - {label}: {_format_due_at(timestamp)}" if timestamp else ""
        lines.append(f"{index}. {assignee} - {task['title']}{client}{when}")
    return lines


def _format_missing_due_date(params: dict) -> str:
    lines = [
        "Entendi a tarefa, mas preciso confirmar o prazo.",
        "",
        f"Tarefa: {params.get('title', 'Tarefa')}",
    ]
    if params.get("assignee_name"):
        lines.append(f"Responsável: {params['assignee_name']}")
    if params.get("client_name"):
        lines.append(f"Cliente: {params['client_name']}")
    lines.extend(
        [
            "",
            "Pode responder naturalmente, por exemplo:",
            "amanhã às 18",
            "sexta-feira às 10",
            "04/05 às 15",
            "",
            "Se preferir deixar sem prazo, responda: sem prazo.",
        ]
    )
    return "\n".join(lines)

def _format_assignee_confirmation(params: dict, heard_name: str, suggestions: list[dict]) -> str:
    lines = [
        "A transcricao parece ter confundido o nome do responsavel.",
        "",
        f"📌 {params.get('title', 'Tarefa')}",
    ]
    if params.get("client_name"):
        lines.append(f"🏢 Cliente: {params['client_name']}")
    if params.get("due_date"):
        lines.append(f"⏰ Prazo: {_format_due_at(params['due_date'])}")
    lines.extend(
        [
            "",
            f"Ouvi: {heard_name}",
            "",
            "Voce quis delegar para:",
        ]
    )
    for index, suggestion in enumerate(suggestions, start=1):
        job_title = suggestion.get("job_title")
        label = f"{suggestion['name']} ({job_title})" if job_title else suggestion["name"]
        lines.append(f"{index}. {label}")
    lines.extend(["", "Responda com o numero da opcao."])
    return "\n".join(lines)


def _format_assignee_not_found(params: dict, heard_name: str) -> str:
    lines = [
        f"Nao encontrei {heard_name} no time.",
        "",
        f"📌 {params.get('title', 'Tarefa')}",
    ]
    if params.get("client_name"):
        lines.append(f"🏢 Cliente: {params['client_name']}")
    lines.extend(
        [
            "",
            "Me envie novamente com o nome do responsavel cadastrado.",
            "Exemplo: Leozin, testar a API da Dairy ate amanha as 18",
        ]
    )
    return "\n".join(lines)


def _format_contextual_fallback(message: str) -> str:
    text = _normalize_text(message)

    if not text:
        return "Me envie uma tarefa, uma consulta ou um comando para eu te ajudar."

    if _looks_like_task_scope_question(text):
        return (
            "Quais tarefas voce quer ver?\n\n"
            "Voce pode responder:\n"
            "minhas tarefas\n"
            "tarefas de hoje\n"
            "tarefas atrasadas\n"
            "tarefas para 04/05"
        )

    if _looks_like_incomplete_invite(text):
        return (
            "Para convidar alguem, voce pode compartilhar o contato aqui na conversa.\n\n"
            "Se preferir digitar, envie assim:\n"
            "convidar Luiz 554198757341 como Desenvolvedor"
        )

    assignee = _extract_addressed_person(message)
    if assignee:
        return (
            f"Entendi que talvez seja para {assignee}, mas faltou clareza na tarefa ou no prazo.\n\n"
            "Me envie em uma frase assim:\n"
            f"{assignee}, revisar contrato da Dairy ate sexta as 18"
        )

    if _contains_task_signal(text):
        return (
            "Entendi que pode ser uma tarefa, mas faltou algum detalhe para eu registrar com seguranca.\n\n"
            "Me envie com responsavel, acao e prazo. Exemplo:\n"
            "Martins, ajustar os prazos no DixCore ate terca as 18"
        )

    if _contains_command_signal(text):
        return (
            "Quase entendi, mas preciso de mais contexto.\n\n"
            "Se quiser concluir: concluir nome da tarefa\n"
            "Se quiser reagendar: reagendar nome da tarefa para amanha as 10"
        )

    return (
        "Quero te ajudar, mas preciso de um pouco mais de contexto.\n\n"
        "Voce quer criar uma tarefa, listar tarefas, concluir, reagendar ou ver o resumo do time?\n"
        "Pode escrever naturalmente. Exemplo: Martins, revisar contrato da Dairy ate sexta as 18"
    )



def _format_invite_missing_job_title(params: dict) -> str:
    name = params.get("name") or "essa pessoa"
    phone = params.get("phone")
    lines = [
        f"Encontrei o contato do {name}.",
    ]
    if phone:
        lines.append(f"Telefone: {phone}")
    lines.extend(
        [
            "",
            "Qual será o cargo ou função dessa pessoa no time?",
            "Exemplos: Desenvolvedor, Gestor Comercial, CEO.",
        ]
    )
    return "\n".join(lines)


def _extract_invite_job_title(message: str) -> str | None:
    normalized = _normalize_text(message)
    no_role = {"sem cargo", "sem funcao", "sem função", "nao sei", "não sei"}
    if normalized in no_role:
        return "Colaborador"
    value = extract_onboarding_value("job_title", message) or message
    value = value.strip(" .,:;\n\t")
    if not _is_valid_onboarding_value(value):
        return None
    if _normalize_text(value) in {"aceitar", "sim", "ok", "pode", "manda", "enviar"}:
        return None
    return value[:60]
def _format_invite_user(result: dict) -> str:
    if not result.get("invited"):
        if result.get("reason") == "permission_denied":
            return "Você não tem permissão para convidar colaboradores."
        return (
            "Claro. Para convidar alguém, compartilhe o contato aqui na conversa.\n\n"
            "Se preferir, também pode digitar nome, telefone e cargo. "
            "Exemplo: convidar Ana 554199999999 como Gerente Comercial."
        )

    lines = [
        f"Convite enviado para {result['name']}.",
        "",
        f"Telefone: {result['phone']}",
    ]
    if result.get("job_title"):
        lines.append(f"Cargo: {result['job_title']}")
    lines.extend(["", "Assim que a pessoa aceitar, vocês poderão trocar delegações pelo Delega AI."])
    return "\n".join(lines)

def _format_pending_invite(invite: dict) -> str:
    company_name = invite.get("company_name") or "sua empresa"
    inviter_name = invite.get("invited_by_name")
    lines = [
        f"Você foi convidado para participar da {company_name} no Delega AI.",
        "",
    ]
    if inviter_name:
        lines.append(f"Convidado por: {inviter_name}")
    if invite.get("name"):
        lines.append(f"Nome: {invite['name']}")
    if invite.get("job_title"):
        lines.append(f"Cargo: {invite['job_title']}")
    lines.extend(
        [
            "",
            "Responda naturalmente: aceitar convite ou recusar.",
        ]
    )
    return "\n".join(lines)

def _format_invite_response_help(invite: dict) -> str:
    company_name = invite.get("company_name") or "a empresa"
    return (
        f"Ainda não consegui confirmar sua resposta ao convite da {company_name}.\n\n"
        "Para entrar, responda naturalmente: aceito, pode confirmar ou quero participar.\n"
        "Para recusar, responda: recusar."
    )
def _format_invite_accepted(context: dict) -> str:
    lines = [
        "Convite aceito.",
        "",
        f"Empresa: {context['company_name']}",
        f"Usuário: {context['user_name']}",
    ]
    if context.get("job_title"):
        lines.append(f"Cargo: {context['job_title']}")
    lines.extend(
        [
            "",
            "Agora vocês já podem trocar delegações pelo Delega AI.",
        ]
    )
    return "\n".join(lines)

def _format_task_line(task: dict) -> str:
    title = task["title"]
    lines = [f"{title}"]
    details = []
    if task.get("assignee_name"):
        details.append(f"Responsável: {task['assignee_name']}")
    if task.get("created_by_name"):
        details.append(f"Criada por: {task['created_by_name']}")
    if task.get("client_name"):
        details.append(f"Cliente: {task['client_name']}")
    if task.get("status") == "done" and task.get("completed_at"):
        details.append(f"Concluída em: {_format_due_at(task['completed_at'])}")
    else:
        details.append(f"Prazo: {_format_due_at(task.get('due_at'))}")
    details.append(f"Status: {_format_task_status(task.get('status'))}")
    lines.extend(f"   {detail}" for detail in details)
    return "\n".join(lines)

def _format_task_status(status: str | None) -> str:
    labels = {
        "pending": "pendente",
        "in_progress": "em andamento",
        "done": "concluída",
        "cancelled": "cancelada",
    }
    return labels.get(status or "", status or "pendente")

def _format_due_at(value: str | None) -> str:
    if not value:
        return "sem prazo"
    try:
        due_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if due_at.tzinfo is not None:
        due_at = due_at.astimezone(APP_TIMEZONE)
    return due_at.strftime("%d/%m as %H:%M")


def _extract_choice_number(message: str) -> int | None:
    text = message.lower().strip()
    from unicodedata import normalize as _unorm

    text_norm = "".join(char for char in _unorm("NFKD", text) if char.isascii())
    ordinal_map: dict[str, int] = {
        "1": 1,
        "2": 2,
        "3": 3,
        "4": 4,
        "5": 5,
        "um": 1,
        "uma": 1,
        "dois": 2,
        "duas": 2,
        "tres": 3,
        "trez": 3,
        "quatro": 4,
        "cinco": 5,
        "primeiro": 1,
        "segundo": 2,
        "terceiro": 3,
        "quarto": 4,
        "quinto": 5,
        "primeira": 1,
        "segunda": 2,
        "terceira": 3,
        "quarta": 4,
        "quinta": 5,
        "o primeiro": 1,
        "o segundo": 2,
        "o terceiro": 3,
        "o quarto": 4,
        "o quinto": 5,
        "a primeira": 1,
        "a segunda": 2,
        "a terceira": 3,
        "a quarta": 4,
        "a quinta": 5,
        "o de cima": 1,
        "a de cima": 1,
    }

    if text_norm in ordinal_map:
        return ordinal_map[text_norm]

    match = re.search(r"\b(?:opcao|numero|item)\s+(\d{1,2})\b", text_norm)
    if match:
        return int(match.group(1))

    match = re.fullmatch(r"\s*(\d{1,2})\s*", text_norm)
    if match:
        return int(match.group(1))

    for term, number in ordinal_map.items():
        if len(term) < 3:
            continue
        if re.search(rf"\b{re.escape(term)}\b", text_norm):
            return number
    return None

def _clean_onboarding_answer(message: str) -> str:
    return message.strip(" \n\t.,;:")


def _is_valid_onboarding_value(message: str) -> bool:
    normalized = _normalize_text(message)
    if len(normalized) < 2:
        return False
    return not _is_greeting(message)

def _detect_received_task_intent(message: str) -> str | None:
    text = _normalize_text(message)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if not text:
        return None

    if any(phrase in text for phrase in ["mais prazo", "novo prazo", "outro prazo", "preciso de prazo", "preciso remarcar", "preciso reagendar"]):
        return "more_time"
    if re.search(r"\b(reagendar|remarcar|adiar)\b", text):
        return "more_time"

    cannot_do_patterns = [
        r"\bnao\s+(vou|consigo|conseguirei|posso)\b",
        r"\bnao\s+vai\s+dar\b",
        r"\bnao\s+e\s+comigo\b",
        r"\bnao\s+consigo\s+fazer\b",
        r"\bimpossivel\b",
    ]
    if any(re.search(pattern, text) for pattern in cannot_do_patterns):
        return "cannot_do"

    help_patterns = [
        r"\bpreciso\s+de\s+ajuda\b",
        r"\bme\s+ajuda\b",
        r"\bconsegue\s+me\s+ajudar\b",
        r"\btenho\s+dificuldade\b",
        r"\btravei\b",
        r"\bnao\s+sei\s+(fazer|resolver|como)\b",
    ]
    if any(re.search(pattern, text) for pattern in help_patterns):
        return "needs_help"

    reassign_patterns = [
        r"\bmanda\s+(?:para|pra|pro)\s+outra\s+pessoa\b",
        r"\brepassa\s+(?:para|pra|pro)\b",
        r"\bpassa\s+(?:para|pra|pro)\b",
        r"\btransferir\s+(?:para|pra|pro)\b",
        r"\bisso\s+e\s+com\s+outra\s+pessoa\b",
        r"\bmelhor\s+outra\s+pessoa\b",
    ]
    if any(re.search(pattern, text) for pattern in reassign_patterns):
        return "reassign_requested"

    details_patterns = [
        r"\bqual\s+(era|e|eh)\b",
        r"\bme\s+(explica|explique|manda|mostra|mostre)\b",
        r"\bexplica\s+(melhor|essa|a tarefa)\b",
        r"\bo\s+que\s+(era|preciso|tenho)\b",
        r"\bnao\s+entendi\b",
        r"\btenho\s+duvida\b",
        r"\bduvida\b",
        r"\bdetalhes\b",
    ]
    if any(re.search(pattern, text) for pattern in details_patterns):
        return "details"

    return None


def _open_tasks_for_user(context: dict) -> list:
    return store.list_tasks(
        company_id=context["company_id"],
        assigned_to=context["user_id"],
        status_filter="pending",
    )


def _task_matches(tasks: list) -> list[dict]:
    return [
        {
            "id": str(task.id) if hasattr(task, "id") else task["id"],
            "title": task.title if hasattr(task, "title") else task["title"],
            "due_at": (task.due_at.isoformat() if getattr(task, "due_at", None) else None) if hasattr(task, "due_at") else task.get("due_at"),
            "client_name": getattr(task, "client_name", None) if hasattr(task, "client_name") else task.get("client_name"),
            "status": (task.status.value if hasattr(getattr(task, "status", None), "value") else getattr(task, "status", None)) if hasattr(task, "status") else task.get("status"),
        }
        for task in sorted(tasks, key=lambda item: getattr(item, "due_at", None) or datetime.max)[:5]
    ]


def _match_task_from_message(message: str, tasks: list):
    text = _normalize_text(message)
    best = None
    best_score = 0
    for task in tasks:
        title = _normalize_text(task.title)
        client = _normalize_text(task.client_name or "")
        score = 0
        for word in set(text.split()):
            if len(word) < 4:
                continue
            if word in title:
                score += 2
            if client and word in client:
                score += 3
        if score > best_score:
            best = task
            best_score = score
    return best if best_score >= 3 else None


def _save_pending_reschedule(context: dict, task) -> None:
    store.save_pending_choice(
        context["company_id"],
        context["user_id"],
        "reschedule_task",
        _task_matches([task]),
        params={"awaiting_due_date": True, "selected_task_id": str(task.id)},
        ttl_minutes=settings.pending_choice_ttl_minutes,
    )


def _is_pending_task_reschedule(pending: dict | None) -> bool:
    return bool(
        pending
        and pending.get("action") == "reschedule_task"
        and (pending.get("params") or {}).get("awaiting_due_date")
    )


def _format_received_task_details(task_or_match) -> str:
    if hasattr(task_or_match, "title"):
        title = task_or_match.title
        due_at = task_or_match.due_at.isoformat() if task_or_match.due_at else None
        client_name = task_or_match.client_name
        status = task_or_match.status.value if hasattr(task_or_match.status, "value") else task_or_match.status
    else:
        title = task_or_match.get("title")
        due_at = task_or_match.get("due_at")
        client_name = task_or_match.get("client_name")
        status = task_or_match.get("status")

    lines = ["Claro. Esta é a tarefa:", "", f"Tarefa: {title}"]
    if client_name:
        lines.append(f"Cliente: {client_name}")
    lines.append(f"Prazo: {_format_due_at(due_at)}")
    lines.append(f"Status: {_format_task_status(status)}")
    lines.extend(["", "Você pode responder naturalmente: comecei a fazer, concluído ou preciso de mais prazo."])
    return "\n".join(lines)


def _format_cannot_do_response(task_or_match) -> str:
    title = task_or_match.title if hasattr(task_or_match, "title") else task_or_match.get("title")
    return (
        "Entendi. Mantive a tarefa aberta e avisei quem criou.\n\n"
        f"Tarefa: {title}\n\n"
        "Se for só prazo, você pode dizer: preciso de mais prazo até amanhã às 18."
    )


def _format_negative_task_response(task_or_match, update_type: str) -> str:
    title = task_or_match.title if hasattr(task_or_match, "title") else task_or_match.get("title")
    if update_type == "needs_help":
        return (
            "Entendi. Mantive a tarefa aberta e avisei quem criou que você precisa de ajuda.\n\n"
            f"Tarefa: {title}"
        )
    if update_type == "reassign_requested":
        return (
            "Entendi. Ainda não transferi automaticamente, mas avisei quem criou que talvez seja melhor repassar.\n\n"
            f"Tarefa: {title}"
        )
    return _format_cannot_do_response(task_or_match)


def _task_update_from_match(context: dict, match: dict, update_type: str) -> dict:
    created_by = match.get("created_by")
    creator_phone = store.get_user_phone(created_by) if created_by else None
    actor_id = context["user_id"]
    return {
        "task_id": match.get("id"),
        "title": match.get("title"),
        "status": match.get("status"),
        "due_at": match.get("due_at"),
        "created_by": created_by,
        "created_by_name": store.get_user_name(created_by) if created_by else None,
        "created_by_phone": creator_phone,
        "actor_id": actor_id,
        "actor_name": context.get("user_name"),
        "task_update_type": update_type,
        "should_notify_task_creator": bool(created_by and created_by != actor_id and creator_phone),
    }


def _is_task_acknowledgement(message: str) -> bool:
    normalized = _normalize_text(message)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    if not normalized:
        return False

    if _is_cancel_intent(message):
        return False
    if any(term in normalized for term in ["conclui", "concluido", "concluida", "feito", "terminei", "finalizei"]):
        return False
    if any(term in normalized for term in ["prazo", "reagendar", "remarcar", "adiar", "mudar"]):
        return False

    short_acknowledgements = {
        "ok",
        "okay",
        "sim",
        "beleza",
        "fechado",
        "combinado",
        "ta bom",
        "esta bom",
        "tudo bem",
        "blz",
        "show",
        "perfeito",
        "certo",
        "tranquilo",
        "aceito",
        "aceitar",
    }
    if normalized in short_acknowledgements:
        return True

    acknowledgement_patterns = [
        r"\b(eu\s+)?aceit\w*\b",
        r"\bpode\s+deixar\b",
        r"\bvou\s+(fazer|ver|ajustar|resolver|cuidar|mexer|olhar|trabalhar|pedir|cobrar|ligar|mandar|enviar|iniciar|comecar|solicitar|verificar|checar|falar|confirmar)\b",
        r"\bja\s+vou\s+(fazer|ver|ajustar|resolver|cuidar|mexer|olhar|pedir|cobrar|ligar|mandar|enviar)\b",
        r"\bvou\s+\w+\s+agora\b",
        r"\bdeixa\s+comigo\b",
        r"\bfico\s+com\s+essa\b",
        r"\besta\s+comigo\b",
        r"\bta\s+comigo\b",
        r"\bcomecei\b",
        r"\biniciei\b",
        r"\bja\s+(comecei|iniciei|estou|to)\b",
        r"\bestou\s+(fazendo|vendo|ajustando|trabalhando|resolvendo|pedindo|cobrando|ligando|mandando|enviando)\b",
        r"\bto\s+(fazendo|vendo|ajustando|trabalhando|resolvendo|pedindo|cobrando)\b",
        r"\bpode\s+contar\s+comigo\b",
        r"\bja\s+resolvi\b",
        r"\bja\s+fiz\b",
    ]
    return any(re.search(pattern, normalized) for pattern in acknowledgement_patterns)

def _is_invite_acceptance(message: str) -> bool:
    normalized = _normalize_invite_response(message)
    if not normalized or _is_invite_decline(message):
        return False

    if normalized in {
        "1",
        "s",
        "sim",
        "ok",
        "okay",
        "aceito",
        "aceitar",
        "confirmo",
        "confirmar",
        "confirmado",
        "pode",
        "claro",
        "beleza",
        "ta bom",
        "tudo bem",
        "bora",
        "vamos la",
    }:
        return True

    acceptance_patterns = [
        r"\b(opcao\s*)?(um|primeiro|primeira)\b",
        r"\baceit\w*\b",
        r"\bconfirm\w*\b",
        r"\bpode\s+(aceitar|confirmar|colocar|entrar|seguir)\b",
        r"\bquero\s+(aceitar|entrar|participar|confirmar)\b",
        r"\bposso\s+(entrar|participar)\b",
        r"\bme\s+coloca\b",
        r"\bcoloca\s+eu\b",
        r"\bparticipar\b",
        r"\bentrar\b",
        r"\bfechado\b",
        r"\bcombinado\b",
    ]
    return any(re.search(pattern, normalized) for pattern in acceptance_patterns)


def _is_invite_decline(message: str) -> bool:
    normalized = _normalize_invite_response(message)
    if not normalized:
        return False

    if normalized in {"2", "n", "nao", "não", "recusar", "recuso", "cancelar"}:
        return True

    decline_patterns = [
        r"\b(opcao\s*)?(dois|segundo|segunda)\b",
        r"\bnao\s+(aceito|quero|posso|vou|da|consigo)\b",
        r"\bnao\s+quero\s+(entrar|participar|aceitar)\b",
        r"\bprefiro\s+nao\b",
        r"\bagora\s+nao\b",
        r"\brecus\w*\b",
        r"\brejeit\w*\b",
        r"\bcancel\w*\b",
    ]
    return any(re.search(pattern, normalized) for pattern in decline_patterns)


def _normalize_invite_response(message: str) -> str:
    normalized = _normalize_text(message)
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _normalize_short_answer(message: str) -> str:
    return _normalize_invite_response(message)

def _normalize_text(value: str) -> str:
    text = normalize("NFKD", value.lower().strip())
    return "".join(char for char in text if char.isascii())


def _extract_addressed_person(message: str) -> str | None:
    if "," not in message:
        return None
    possible_name = message.split(",", 1)[0].strip(" \n\t.,;:")
    if not possible_name or len(possible_name.split()) > 3:
        return None
    return possible_name


def _looks_like_task_scope_question(text: str) -> bool:
    has_task_word = any(word in text for word in ["tarefa", "tarefas", "pendencia", "pendencias"])
    has_action_word = any(word in text for word in ["ver", "listar", "mostrar", "mostra", "consultar", "quais"])
    return has_task_word and (has_action_word or text in {"tarefa", "tarefas", "pendencia", "pendencias"})


def _looks_like_incomplete_invite(text: str) -> bool:
    return any(word in text for word in ["convidar", "convide", "adicionar pessoa", "adicionar colaborador"])


def _contains_task_signal(text: str) -> bool:
    task_words = [
        "fazer",
        "criar",
        "ajustar",
        "revisar",
        "enviar",
        "subir",
        "validar",
        "resolver",
        "cliente",
        "prazo",
        "ate",
    ]
    return any(word in text for word in task_words)


def _contains_command_signal(text: str) -> bool:
    return any(word in text for word in ["concluir", "feito", "finalizar", "reagendar", "remarcar"])


def _is_greeting(message: str) -> bool:
    normalized = message.strip().lower()
    return normalized in {"oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "hello", "start"}


def _is_paginate_intent(message: str) -> bool:
    normalized = _normalize_text(message).strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    paginate_phrases = {
        "proximas",
        "proximas tarefas",
        "ver mais",
        "mais tarefas",
        "continuar",
        "proximo",
        "proxima pagina",
        "mais",
        "proximo pagina",
        "ver mais tarefas",
        "mostrar mais",
        "mostrar mais tarefas",
        "carregar mais",
    }
    return normalized in paginate_phrases


def _is_cancel_intent(message: str) -> bool:
    normalized = _normalize_text(message).strip()
    cancel_words = {"cancelar", "cancela", "esquecer", "esquece", "esqueca", "desistir", "desisto", "parar", "para", "nao quero", "nao precisa", "deixa pra la", "deixa para la"}
    return normalized in cancel_words


def _is_no_due_date_intent(message: str) -> bool:
    normalized = _normalize_text(message).strip(" \t\n\r.,;:!?")
    normalized = " ".join(normalized.split())
    no_due_phrases = {
        "sem prazo",
        "deixa sem prazo",
        "pode deixar sem prazo",
        "sem data",
        "sem data limite",
        "sem deadline",
        "nao tem prazo",
        "nao precisa de prazo",
        "nao precisa prazo",
        "depois defino",
        "defino depois",
        "coloca sem prazo",
    }
    return normalized in no_due_phrases


def _is_date_completion(message: str) -> bool:
    if parse_due_date(message) is None:
        return False
    normalized = _normalize_text(message)
    words = normalized.split()
    if len(words) > 7:
        return False
    # Conjunctions, personal verbs, and narrative markers indicate the date is
    # embedded in a sentence rather than being a standalone reply to "when?"
    non_date_markers = {
        "que", "porque", "pois", "mas", "quando", "se", "como", "para", "quero", "preciso",
        "tenho", "tem", "temos", "vou", "vai", "vamos", "sera", "serei", "terei",
        "estou", "esta", "estamos", "e", "sao", "fui", "foi",
        "reuniao", "compromisso", "viagem", "evento",
    }
    if any(w in non_date_markers for w in words):
        return False
    return not _has_explicit_action_intent(message)


def _has_explicit_action_intent(message: str) -> bool:
    normalized = _normalize_text(message)
    action_words = {
        "criar", "crie", "cria", "cadastrar", "adicionar",
        "concluir", "concluido", "conclui", "feito", "finalizar", "completo",
        "listar", "lista", "minhas", "tarefas", "ver tarefas",
        "reagendar", "remarcar", "adiar", "mudar prazo",
        "convidar", "convite",
        "resumo", "relatorio", "time",
    }
    return any(word in normalized for word in action_words)


def log_interaction(state: AgentState) -> AgentState:
    context = state.get("context")
    parsed = state.get("parsed")
    if not context:
        return {}

    store.log_whatsapp_message(
        company_id=context["company_id"],
        user_id=context["user_id"],
        provider_message_id=state.get("provider_message_id"),
        body=state["message"],
        parsed=parsed.model_dump(mode="json") if parsed else {},
        response_body=state.get("reply", ""),
        error=state.get("error"),
        from_phone=state.get("from_phone", ""),
    )
    return {}


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("identify_context", identify_context)
    graph.add_node("parse_intent", parse_intent)
    graph.add_node("create_task", execute_create_task)
    graph.add_node("list_my_tasks", execute_list_my_tasks)
    graph.add_node("complete_task", execute_complete_task)
    graph.add_node("reschedule_task", execute_reschedule_task)
    graph.add_node("edit_task", execute_edit_task)
    graph.add_node("cancel_task", execute_cancel_task)
    graph.add_node("task_status", execute_task_status)
    graph.add_node("team_summary", execute_team_summary)
    graph.add_node("invite_user", execute_invite_user)
    graph.add_node("edit_member", execute_edit_member)
    graph.add_node("resolve_pending_choice", execute_pending_choice)
    graph.add_node("complete_pending_invite_draft", complete_pending_invite_draft)
    graph.add_node("complete_pending_task_draft", complete_pending_task_draft)
    graph.add_node("complete_pending_task_reschedule", complete_pending_task_reschedule)
    graph.add_node("handle_received_task_reply", handle_received_task_reply)
    graph.add_node("acknowledge_task", acknowledge_task)
    graph.add_node("acknowledge_task_completion", acknowledge_task_completion)
    graph.add_node("start_task", execute_start_task)
    graph.add_node("handle_invite_response", handle_invite_response)
    graph.add_node("handle_onboarding", handle_onboarding)
    graph.add_node("handle_contact_invite", handle_contact_invite)
    graph.add_node("handle_cancelled_state", handle_cancelled_state)
    graph.add_node("paginate_tasks", execute_paginate_tasks)
    graph.add_node("fallback", fallback)
    graph.add_node("format_reply", format_reply)
    graph.add_node("log_interaction", log_interaction)

    graph.add_edge(START, "identify_context")
    graph.add_conditional_edges(
        "identify_context",
        route_after_context,
        {
            "parse_intent": "parse_intent",
            "format_reply": "format_reply",
            "resolve_pending_choice": "resolve_pending_choice",
            "complete_pending_invite_draft": "complete_pending_invite_draft",
            "complete_pending_task_draft": "complete_pending_task_draft",
            "complete_pending_task_reschedule": "complete_pending_task_reschedule",
            "handle_invite_response": "handle_invite_response",
            "handle_onboarding": "handle_onboarding",
            "handle_contact_invite": "handle_contact_invite",
            "handle_cancelled_state": "handle_cancelled_state",
            "paginate_tasks": "paginate_tasks",
        },
    )
    graph.add_conditional_edges(
        "parse_intent",
        route_action,
        {
            "create_task": "create_task",
            "list_my_tasks": "list_my_tasks",
            "complete_task": "complete_task",
            "acknowledge_task_completion": "acknowledge_task_completion",
            "start_task": "start_task",
            "reschedule_task": "reschedule_task",
            "edit_task": "edit_task",
            "cancel_task": "cancel_task",
            "task_status": "task_status",
            "team_summary": "team_summary",
            "invite_user": "invite_user",
            "edit_member": "edit_member",
            "acknowledge_task": "acknowledge_task",
            "handle_received_task_reply": "handle_received_task_reply",
            "fallback": "fallback",
        },
    )
    graph.add_edge("create_task", "format_reply")
    graph.add_edge("list_my_tasks", "format_reply")
    graph.add_edge("complete_task", "format_reply")
    graph.add_edge("acknowledge_task_completion", "log_interaction")
    graph.add_edge("start_task", "format_reply")
    graph.add_edge("reschedule_task", "format_reply")
    graph.add_edge("edit_task", "format_reply")
    graph.add_edge("cancel_task", "format_reply")
    graph.add_edge("task_status", "format_reply")
    graph.add_edge("team_summary", "format_reply")
    graph.add_edge("invite_user", "format_reply")
    graph.add_edge("edit_member", "format_reply")
    graph.add_edge("resolve_pending_choice", "format_reply")
    graph.add_edge("complete_pending_invite_draft", "format_reply")
    graph.add_edge("complete_pending_task_draft", "format_reply")
    graph.add_edge("complete_pending_task_reschedule", "log_interaction")
    graph.add_edge("handle_received_task_reply", "log_interaction")
    graph.add_edge("acknowledge_task", "log_interaction")
    graph.add_edge("handle_invite_response", "log_interaction")
    graph.add_edge("handle_onboarding", "log_interaction")
    graph.add_edge("handle_contact_invite", "format_reply")
    graph.add_edge("handle_cancelled_state", "log_interaction")
    graph.add_edge("paginate_tasks", "format_reply")
    graph.add_edge("fallback", "format_reply")
    graph.add_edge("format_reply", "log_interaction")
    graph.add_edge("log_interaction", END)

    return graph.compile()


app_graph = build_graph()



































