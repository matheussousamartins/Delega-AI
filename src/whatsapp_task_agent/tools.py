from datetime import datetime
from difflib import SequenceMatcher
from typing import Any
from unicodedata import normalize
from zoneinfo import ZoneInfo

from whatsapp_task_agent.schemas import InviteStatus
from whatsapp_task_agent.schemas import CompanyContext
from whatsapp_task_agent.store import store

APP_TIMEZONE = ZoneInfo("America/Sao_Paulo")
PAGE_SIZE = 10


def create_task(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    task = store.create_task(
        company_id=context["company_id"],
        created_by=context["user_id"],
        title=params["title"],
        assignee_name=params.get("assignee_name"),
        priority=params.get("priority"),
        due_at=_parse_datetime(params.get("due_date")),
        description=params.get("description"),
        client_name=params.get("client_name"),
    )
    assigned_to_self = str(task.assigned_to) == context["user_id"]
    return {
        "task_id": str(task.id),
        "short_id": str(task.id)[:8],
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "assigned_to": str(task.assigned_to),
        "assignee_name": store.get_user_name(str(task.assigned_to)),
        "assignee_phone": store.get_user_phone(str(task.assigned_to)),
        "client_id": str(task.client_id) if task.client_id else None,
        "client_name": task.client_name,
        "created_by_name": context["user_name"],
        "assigned_to_self": assigned_to_self,
        "should_notify_assignee": not assigned_to_self,
    }


def find_assignee_suggestions(context: CompanyContext, assignee_name: str | None) -> list[dict[str, Any]]:
    if not assignee_name:
        return []
    candidate = _normalize_name(assignee_name)
    if len(candidate) < 3:
        return []

    suggestions = []
    for member in store.list_company_members(context["company_id"]):
        member_name = member.get("name") or ""
        score = _name_similarity(candidate, _normalize_name(member_name))
        if score >= 0.35:
            suggestions.append(
                {
                    "id": member["id"],
                    "name": member_name,
                    "job_title": member.get("job_title"),
                    "score": score,
                }
            )
    return sorted(suggestions, key=lambda item: item["score"], reverse=True)[:3]


def list_my_tasks(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    view = params.get("view", "my")
    status_filter = params.get("filter", "pending")
    client_name = params.get("client_name") or None
    page = max(1, int(params.get("page") or 1))
    offset = (page - 1) * PAGE_SIZE

    if view == "delegated":
        all_delegated = store.list_delegated_tasks(
            company_id=context["company_id"],
            created_by=context["user_id"],
            status_filter=status_filter,
            client_name=client_name,
        )
        delegated_page = all_delegated[offset : offset + PAGE_SIZE]
        has_more = (offset + PAGE_SIZE) < len(all_delegated)
        return {
            "filter": status_filter,
            "view": "delegated",
            "client_name": client_name,
            "page": page,
            "total_tasks": 0,
            "total_delegated": len(all_delegated),
            "has_more": has_more,
            "tasks": [],
            "delegated_tasks": [_task_row(task) for task in delegated_page],
        }

    all_tasks = store.list_tasks(
        company_id=context["company_id"],
        assigned_to=context["user_id"],
        status_filter=status_filter,
        target_date=_parse_date(params.get("date")),
        client_name=client_name,
    )

    if view == "all":
        all_delegated = store.list_delegated_tasks(
            company_id=context["company_id"],
            created_by=context["user_id"],
            status_filter=status_filter,
            client_name=client_name,
        )
        tasks_page = all_tasks[offset : offset + PAGE_SIZE]
        delegated_page = all_delegated[offset : offset + PAGE_SIZE]
        has_more = (offset + PAGE_SIZE) < len(all_tasks) or (offset + PAGE_SIZE) < len(all_delegated)
        return {
            "filter": status_filter,
            "view": "all",
            "date": params.get("date"),
            "client_name": client_name,
            "page": page,
            "total_tasks": len(all_tasks),
            "total_delegated": len(all_delegated),
            "has_more": has_more,
            "tasks": [_task_row(task) for task in tasks_page],
            "delegated_tasks": [_task_row(task) for task in delegated_page],
        }

    tasks_page = all_tasks[offset : offset + PAGE_SIZE]
    has_more = (offset + PAGE_SIZE) < len(all_tasks)
    return {
        "filter": status_filter,
        "view": "my",
        "date": params.get("date"),
        "client_name": client_name,
        "page": page,
        "total_tasks": len(all_tasks),
        "total_delegated": 0,
        "has_more": has_more,
        "tasks": [_task_row(task) for task in tasks_page],
        "delegated_tasks": [],
    }


def _task_row(task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "short_id": str(task.id)[:8],
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "client_name": task.client_name,
        "assignee_name": store.get_user_name(str(task.assigned_to)),
        "created_by_name": store.get_user_name(str(task.created_by)),
    }


def task_status(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    status_filter = params.get("status_filter") or "open"
    tasks = store.list_visible_tasks(
        company_id=context["company_id"],
        requester_user_id=context["user_id"],
        requester_role=context["role"],
        status_filter=status_filter,
    )

    member_name = params.get("member_name")
    member_id = store.find_user_by_name(context["company_id"], member_name) if member_name else None
    if member_name and member_id is None:
        return {"found": False, "reason": "member_not_found", "member_name": member_name, "tasks": []}
    if member_id:
        tasks = [task for task in tasks if str(task.assigned_to) == str(member_id)]

    client_name = params.get("client_name")
    if client_name:
        normalized_client = _normalize_name(str(client_name))
        tasks = [
            task
            for task in tasks
            if task.client_name and _name_similarity(normalized_client, _normalize_name(task.client_name)) >= 0.65
        ]

    task_reference = _clean_task_reference(str(params.get("task_reference") or ""))
    if task_reference:
        normalized_reference = _normalize_name(task_reference)
        scored = [
            (task, max(_task_similarity(normalized_reference, task.title), _task_similarity(normalized_reference, task.client_name or "")))
            for task in tasks
        ]
        tasks = [task for task, score in sorted(scored, key=lambda item: -item[1]) if score >= 0.45]

    return {
        "found": bool(tasks),
        "query": {
            "member_name": member_name,
            "client_name": client_name,
            "task_reference": task_reference or None,
            "status_filter": status_filter,
        },
        "tasks": [_visible_task_summary(task) for task in tasks[:8]],
        "total": len(tasks),
    }


def complete_task(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    task = store.complete_task(
        company_id=context["company_id"],
        user_id=context["user_id"],
        task_reference=_clean_task_reference(params["task_reference"]),
    )
    if task is None:
        return {"found": False}
    if isinstance(task, list):
        matches = [_task_summary(item) for item in task]
        store.save_pending_choice(
            company_id=context["company_id"],
            user_id=context["user_id"],
            action="complete_task",
            matches=matches,
            params={},
        )
        return {"found": True, "ambiguous": True, "matches": matches}
    return {"found": True, **task_update_payload(context, task, "completed")}


def start_task(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    task = store.start_task(
        company_id=context["company_id"],
        user_id=context["user_id"],
        task_reference=_clean_task_reference(params["task_reference"]),
    )
    if task is None:
        return {"found": False}
    if isinstance(task, list):
        matches = [_task_summary(item) for item in task]
        store.save_pending_choice(
            company_id=context["company_id"],
            user_id=context["user_id"],
            action="start_task",
            matches=matches,
            params={},
        )
        return {"found": True, "ambiguous": True, "matches": matches}
    return {"found": True, **task_update_payload(context, task, "started")}


def reschedule_task(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    due_at = _parse_datetime(params.get("due_date"))
    if due_at is None:
        return {"rescheduled": False, "reason": "missing_due_date"}
    task = store.reschedule_task(
        company_id=context["company_id"],
        user_id=context["user_id"],
        task_reference=_clean_task_reference(params["task_reference"]),
        due_at=due_at,
    )
    if task is None:
        return {"rescheduled": False, "reason": "not_found"}
    if isinstance(task, list):
        matches = [_task_summary(item) for item in task]
        store.save_pending_choice(
            company_id=context["company_id"],
            user_id=context["user_id"],
            action="reschedule_task",
            matches=matches,
            params={"due_date": params.get("due_date")},
        )
        return {"rescheduled": False, "reason": "ambiguous", "matches": matches}
    return {"rescheduled": True, **task_update_payload(context, task, "rescheduled")}


def team_summary(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    return store.team_summary(
        company_id=context["company_id"],
        member_name=params.get("member_name"),
    ) | {
        "period": params.get("period", "today"),
        "view": params.get("view", "summary"),
        "member_name": params.get("member_name"),
    }


def invite_user(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    if context["role"] not in {"owner", "admin", "manager"}:
        return {"invited": False, "reason": "permission_denied"}

    name = params.get("name")
    phone = params.get("phone")
    if not name or not phone:
        return {"invited": False, "reason": "missing_name_or_phone"}

    invite = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone=phone,
        name=name,
        job_title=params.get("job_title"),
        role=params.get("role") or "member",
    )
    return {
        "invited": True,
        "invite_id": invite["id"],
        "company_id": context["company_id"],
        "company_name": context.get("company_name"),
        "invited_by": context["user_id"],
        "invited_by_name": context["user_name"],
        "name": invite["name"],
        "phone": invite["phone"],
        "job_title": invite["job_title"],
        "role": invite["role"],
        "status": InviteStatus.pending.value,
        "should_notify_invitee": True,
    }


def resolve_pending_choice(context: CompanyContext, choice_number: int) -> dict[str, Any]:
    pending = store.get_pending_choice(context["company_id"], context["user_id"])
    if pending is None:
        return {"resolved": False, "reason": "not_found"}

    matches = pending.get("matches", [])
    if choice_number < 1 or choice_number > len(matches):
        return {
            "resolved": False,
            "reason": "invalid_choice",
            "available_count": len(matches),
        }

    selected = matches[choice_number - 1]
    action = pending.get("action")

    if action == "complete_task":
        task = store.complete_task_by_id(
            company_id=context["company_id"],
            user_id=context["user_id"],
            task_id=selected["id"],
        )
        if task is None:
            store.clear_pending_choice(context["company_id"], context["user_id"])
            return {"resolved": False, "reason": "task_not_found"}
        store.clear_pending_choice(context["company_id"], context["user_id"])
        return {"resolved": True, "action": action, **task_update_payload(context, task, "completed")}

    if action == "reschedule_task":
        due_at = _parse_datetime((pending.get("params") or {}).get("due_date"))
        if due_at is None:
            return {"resolved": False, "reason": "missing_due_date"}
        task = store.reschedule_task_by_id(
            company_id=context["company_id"],
            user_id=context["user_id"],
            task_id=selected["id"],
            due_at=due_at,
        )
        if task is None:
            store.clear_pending_choice(context["company_id"], context["user_id"])
            return {"resolved": False, "reason": "task_not_found"}
        store.clear_pending_choice(context["company_id"], context["user_id"])
        return {"resolved": True, "action": action, **task_update_payload(context, task, "rescheduled")}

    if action == "start_task":
        task = store.start_task_by_id(
            company_id=context["company_id"],
            user_id=context["user_id"],
            task_id=selected["id"],
        )
        if task is None:
            store.clear_pending_choice(context["company_id"], context["user_id"])
            return {"resolved": False, "reason": "task_not_found"}
        store.clear_pending_choice(context["company_id"], context["user_id"])
        return {"resolved": True, "action": action, **task_update_payload(context, task, "started")}

    if action == "confirm_create_task_assignee":
        params = dict(pending.get("params") or {})
        params["assignee_name"] = selected["name"]
        store.clear_pending_choice(context["company_id"], context["user_id"])
        result = create_task(context, params)
        result["resolved"] = True
        result["action"] = action
        return result

    if action == "cancel_task":
        task = store.cancel_task_by_id(
            company_id=context["company_id"],
            user_id=context["user_id"],
            task_id=selected["id"],
        )
        store.clear_pending_choice(context["company_id"], context["user_id"])
        if task == "permission_denied":
            return {"resolved": False, "reason": "permission_denied"}
        if task is None:
            return {"resolved": False, "reason": "task_not_found"}
        return {"resolved": True, "action": action, "cancelled": True, **task_update_payload(context, task, "cancelled")}

    if action == "clear_task_due_date":
        task = store.clear_task_due_date_by_id(
            company_id=context["company_id"],
            user_id=context["user_id"],
            task_id=selected["id"],
        )
        store.clear_pending_choice(context["company_id"], context["user_id"])
        if task == "permission_denied":
            return {"resolved": False, "reason": "permission_denied"}
        if task is None:
            return {"resolved": False, "reason": "task_not_found"}
        return {"resolved": True, "action": action, "cleared": True, **task_update_payload(context, task, "due_date_cleared")}

    if action == "edit_task":
        params = dict(pending.get("params") or {})
        task = store.edit_task_by_id(
            company_id=context["company_id"],
            user_id=context["user_id"],
            task_id=selected["id"],
            new_title=params.get("new_title"),
            new_assignee_name=params.get("new_assignee_name"),
            new_client_name=params.get("new_client_name"),
        )
        store.clear_pending_choice(context["company_id"], context["user_id"])
        if task is None:
            return {"resolved": False, "reason": "task_not_found"}
        return {"resolved": True, "action": action, "edited": True, **task_update_payload(context, task, "edited")}

    store.clear_pending_choice(context["company_id"], context["user_id"])
    return {"resolved": False, "reason": "unsupported_action"}


def task_update_payload(context: CompanyContext, task, update_type: str) -> dict[str, Any]:
    created_by = str(task.created_by)
    assigned_to = str(task.assigned_to)
    actor_id = context["user_id"]
    creator_phone = store.get_user_phone(created_by)
    assignee_phone = store.get_user_phone(assigned_to)
    notify_creator = created_by != actor_id and bool(creator_phone)
    notify_assignee = actor_id == created_by and assigned_to != actor_id and bool(assignee_phone)
    return {
        "task_id": str(task.id),
        "title": task.title,
        "status": task.status,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "created_by": created_by,
        "created_by_name": store.get_user_name(created_by),
        "created_by_phone": creator_phone,
        "assigned_to": assigned_to,
        "assignee_name": store.get_user_name(assigned_to),
        "assignee_phone": assignee_phone,
        "actor_id": actor_id,
        "actor_name": context.get("user_name"),
        "task_update_type": update_type,
        "should_notify_task_creator": notify_creator,
        "should_notify_task_assignee": notify_assignee,
    }


def cancel_task(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    task_reference = _clean_task_reference(params.get("task_reference") or "")
    if not task_reference:
        return {"cancelled": False, "reason": "missing_task_reference"}

    task = store.cancel_task(
        company_id=context["company_id"],
        user_id=context["user_id"],
        task_reference=task_reference,
    )
    if task == "permission_denied":
        return {"cancelled": False, "reason": "permission_denied"}
    if task is None:
        return {"cancelled": False, "reason": "not_found"}
    if isinstance(task, list):
        matches = [_task_summary(item) for item in task]
        store.save_pending_choice(
            company_id=context["company_id"],
            user_id=context["user_id"],
            action="cancel_task",
            matches=matches,
            params=params,
        )
        return {"cancelled": False, "reason": "ambiguous", "matches": matches}
    return {"cancelled": True, **task_update_payload(context, task, "cancelled")}


def clear_task_due_date(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    task_reference = _clean_task_reference(params.get("task_reference") or "")

    if not task_reference:
        tasks = store.list_delegated_tasks(
            company_id=context["company_id"],
            created_by=context["user_id"],
            status_filter="pending",
        )
        if len(tasks) == 1:
            task = store.clear_task_due_date_by_id(
                company_id=context["company_id"],
                user_id=context["user_id"],
                task_id=str(tasks[0].id),
            )
            if task and task != "permission_denied":
                return {"cleared": True, **task_update_payload(context, task, "due_date_cleared")}
        return {"cleared": False, "reason": "missing_task_reference"}

    task = store.clear_task_due_date(
        company_id=context["company_id"],
        user_id=context["user_id"],
        task_reference=task_reference,
    )
    if task == "permission_denied":
        return {"cleared": False, "reason": "permission_denied"}
    if task is None:
        return {"cleared": False, "reason": "not_found"}
    if isinstance(task, list):
        matches = [_task_summary(item) for item in task]
        store.save_pending_choice(
            company_id=context["company_id"],
            user_id=context["user_id"],
            action="clear_task_due_date",
            matches=matches,
            params=params,
        )
        return {"cleared": False, "reason": "ambiguous", "matches": matches}
    return {"cleared": True, **task_update_payload(context, task, "due_date_cleared")}


def edit_task(context: CompanyContext, params: dict[str, Any]) -> dict[str, Any]:
    task_reference = _clean_task_reference(params.get("task_reference") or "")
    if not task_reference:
        return {"edited": False, "reason": "missing_task_reference"}

    new_assignee_name = params.get("new_assignee_name")
    new_client_name = params.get("new_client_name")
    new_title = params.get("new_title")

    if not any([new_title, new_assignee_name, new_client_name is not None]):
        return {"edited": False, "reason": "nothing_to_change"}

    task = store.edit_task(
        company_id=context["company_id"],
        user_id=context["user_id"],
        task_reference=task_reference,
        new_title=new_title,
        new_assignee_name=new_assignee_name,
        new_client_name=new_client_name,
    )
    if task is None:
        return {"edited": False, "reason": "not_found"}
    if isinstance(task, list):
        matches = [_task_summary(item) for item in task]
        store.save_pending_choice(
            company_id=context["company_id"],
            user_id=context["user_id"],
            action="edit_task",
            matches=matches,
            params=params,
        )
        return {"edited": False, "reason": "ambiguous", "matches": matches}

    return {"edited": True, **task_update_payload(context, task, "edited")}


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=APP_TIMEZONE)
    return parsed


def _parse_date(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value).date()


def _clean_task_reference(value: str) -> str:
    reference = value.strip(" .,:;")
    for prefix in ["tarefa ", "a tarefa ", "da tarefa "]:
        if reference.lower().startswith(prefix):
            return reference[len(prefix) :].strip(" .,:;")
    return reference


def _normalize_name(value: str) -> str:
    text = normalize("NFKD", value.lower().strip())
    ascii_text = "".join(char for char in text if char.isascii())
    return " ".join(ascii_text.split())


def _name_similarity(candidate: str, existing: str) -> float:
    if not candidate or not existing:
        return 0.0
    if candidate == existing:
        return 1.0
    if candidate.startswith(existing) or existing.startswith(candidate):
        return 0.92
    score = SequenceMatcher(None, candidate, existing).ratio()
    if candidate[:1] == existing[:1] and abs(len(candidate) - len(existing)) <= 3:
        score = max(score, 0.42)
    return score


def _task_similarity(reference: str, text: str) -> float:
    candidate = _normalize_name(text)
    if not reference or not candidate:
        return 0.0
    if reference in candidate or candidate in reference:
        return 1.0
    reference_words = reference.split()
    candidate_words = candidate.split()
    if not reference_words or not candidate_words:
        return 0.0
    return sum(max(SequenceMatcher(None, rw, cw).ratio() for cw in candidate_words) for rw in reference_words) / len(reference_words)


def _visible_task_summary(task) -> dict[str, Any]:
    status = task.status.value if hasattr(task.status, "value") else task.status
    return {
        "id": str(task.id),
        "short_id": str(task.id)[:8],
        "title": task.title,
        "status": status,
        "priority": task.priority.value if hasattr(task.priority, "value") else task.priority,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "client_name": task.client_name,
        "assignee_name": store.get_user_name(str(task.assigned_to)),
        "created_by_name": store.get_user_name(str(task.created_by)),
    }


def _task_summary(task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "short_id": str(task.id)[:8],
        "title": task.title,
        "due_at": task.due_at.isoformat() if task.due_at else None,
    }







