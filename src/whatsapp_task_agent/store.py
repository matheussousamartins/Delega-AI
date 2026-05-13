from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from unicodedata import normalize
from uuid import UUID, uuid4

from whatsapp_task_agent.phone import normalize_phone, phone_variants
from whatsapp_task_agent.schemas import InviteStatus, Priority, ReminderCandidate, ReminderKind, Task, TaskStatus
from whatsapp_task_agent.settings import settings


class InMemoryTaskStore:
    def __init__(self) -> None:
        self.company_id = uuid4()
        self.company_name = "Commandix"
        self.owner_id = uuid4()
        self.joao_id = uuid4()
        self.maria_id = uuid4()
        self.users = {
            self.owner_id: "Ana",
            self.joao_id: "Joao",
            self.maria_id: "Maria",
        }
        self.user_job_titles = {
            self.owner_id: "Owner",
            self.joao_id: None,
            self.maria_id: None,
        }
        self.user_roles = {
            self.owner_id: "owner",
            self.joao_id: "member",
            self.maria_id: "member",
        }
        self.user_phones = {
            self.owner_id: "+5511999999999",
            self.joao_id: "+5511988888888",
            self.maria_id: "+5511977777777",
        }
        self.phone_to_user = {
            "+5511999999999": self.owner_id,
            "+5511988888888": self.joao_id,
            "+5511977777777": self.maria_id,
        }
        self.clients: dict[UUID, str] = {}
        self.tasks: dict[UUID, Task] = {}
        self.messages: list[dict] = []
        self.sent_reminders: set[tuple[UUID, ReminderKind]] = set()
        self.pending_choices: dict[tuple[str, str], dict] = {}
        self.pending_task_drafts: dict[tuple[str, str], dict] = {}
        self.pending_invite_drafts: dict[tuple[str, str], dict] = {}
        self.notification_outbox: dict[UUID, dict] = {}
        self.onboarding_sessions: dict[str, dict] = {}
        self.invites: dict[UUID, dict] = {}

    def identify_user(self, from_phone: str) -> dict[str, str]:
        user_id = None
        for phone in phone_variants(from_phone):
            user_id = self.phone_to_user.get(phone)
            if user_id is not None:
                break
        if user_id is None:
            raise ValueError("phone_not_registered")
        return {
            "company_id": str(self.company_id),
            "company_name": self.company_name,
            "user_id": str(user_id),
            "user_name": self.users[user_id],
            "role": self.user_roles.get(user_id, "member"),
        }

    def find_user_by_name(self, company_id_or_name: str | None, name: str | None = None) -> UUID | None:
        if name is None:
            name = company_id_or_name
        if not name:
            return None

        normalized = _normalize(name)
        exact_matches = [uid for uid, uname in self.users.items() if _normalize(uname) == normalized]
        if len(exact_matches) > 1:
            return None  # ambiguous — let find_assignee_suggestions handle disambiguation
        if len(exact_matches) == 1:
            return exact_matches[0]
        for user_id, user_name in self.users.items():
            existing = _normalize(user_name)
            if _is_likely_same_person_name(normalized, existing):
                return user_id

        return None

    def list_company_members(self, company_id: str) -> list[dict]:
        return [
            {
                "id": str(user_id),
                "name": name,
                "phone": self.user_phones.get(user_id),
                "role": self.user_roles.get(user_id, "member"),
                "job_title": self.user_job_titles.get(user_id),
            }
            for user_id, name in self.users.items()
        ]

    def list_clients(self, company_id: str) -> list[dict]:
        return [{"id": str(client_id), "name": name} for client_id, name in self.clients.items()]

    def update_client_name(self, company_id: str, current_name: str, new_name: str) -> dict | None:
        if not current_name or not new_name:
            return None
        current_normalized = _normalize(current_name)
        new_normalized = _normalize(new_name)
        old_id = next(
            (client_id for client_id, existing in self.clients.items() if _normalize(existing) == current_normalized),
            None,
        )
        if old_id is None:
            return None

        target_id = next(
            (
                client_id
                for client_id, existing in self.clients.items()
                if client_id != old_id and _normalize(existing) == new_normalized
            ),
            None,
        )
        affected_tasks = [task for task in self.tasks.values() if task.client_id == old_id]
        merged = target_id is not None
        if target_id is not None:
            self.clients[target_id] = new_name
            self.clients.pop(old_id, None)
            for task in affected_tasks:
                task.client_id = target_id
                task.client_name = new_name
        else:
            self.clients[old_id] = new_name
            for task in affected_tasks:
                task.client_name = new_name

        return {
            "renamed": True,
            "old_name": current_name,
            "new_name": new_name,
            "affected_tasks": len(affected_tasks),
            "merged": merged,
        }

    def get_user_name(self, user_id: str) -> str | None:
        try:
            return self.users.get(UUID(user_id))
        except ValueError:
            return None

    def get_user_phone(self, user_id: str) -> str | None:
        try:
            return self.user_phones.get(UUID(user_id))
        except ValueError:
            return None

    def update_member_name(self, company_id: str, target_user_id: str, new_name: str) -> bool:
        try:
            uid = UUID(target_user_id)
        except ValueError:
            return False
        if uid not in self.users:
            return False
        self.users[uid] = new_name
        return True

    def update_member_job_title(self, company_id: str, target_user_id: str, new_job_title: str) -> bool:
        try:
            uid = UUID(target_user_id)
        except ValueError:
            return False
        if uid not in self.users:
            return False
        self.user_job_titles[uid] = new_job_title
        return True

    def create_task(
        self,
        company_id: str,
        created_by: str,
        title: str,
        assignee_name: str | None = None,
        priority: str | None = None,
        due_at: datetime | None = None,
        description: str | None = None,
        client_name: str | None = None,
    ) -> Task:
        assigned_to = self.find_user_by_name(assignee_name) or UUID(created_by)
        client_id = self.find_or_create_client(company_id, client_name)
        task = Task(
            id=uuid4(),
            company_id=UUID(company_id),
            title=title,
            assigned_to=assigned_to,
            created_by=UUID(created_by),
            client_id=client_id,
            client_name=self.clients.get(client_id) if client_id else None,
            priority=Priority(priority) if priority else None,
            due_at=due_at,
            description=description,
        )
        self.tasks[task.id] = task
        return task

    def find_or_create_client(self, company_id: str, client_name: str | None) -> UUID | None:
        if not client_name:
            return None
        normalized = _normalize(client_name)
        for client_id, existing_name in self.clients.items():
            if _normalize(existing_name) == normalized:
                return client_id
        client_id = uuid4()
        self.clients[client_id] = client_name
        return client_id

    def list_tasks(
        self,
        company_id: str,
        assigned_to: str,
        status_filter: str = "pending",
        target_date: date | None = None,
        client_name: str | None = None,
    ) -> list[Task]:
        tasks = [
            task
            for task in self.tasks.values()
            if str(task.company_id) == company_id and str(task.assigned_to) == assigned_to
        ]
        if status_filter in {"pending", "today"}:
            tasks = [task for task in tasks if task.status in {TaskStatus.pending, TaskStatus.in_progress}]
        if status_filter == "today":
            today_start = datetime.combine(datetime.now().date(), time.min)
            today_end = datetime.combine(datetime.now().date(), time.max)
            tasks = [task for task in tasks if task.due_at and today_start <= task.due_at <= today_end]
        if status_filter == "overdue":
            now = datetime.now()
            tasks = [
                task
                for task in tasks
                if task.status in {TaskStatus.pending, TaskStatus.in_progress}
                and task.due_at is not None
                and _as_naive_datetime(task.due_at) < now
            ]
        if status_filter == "date" and target_date is not None:
            tasks = [
                task
                for task in tasks
                if task.status in {TaskStatus.pending, TaskStatus.in_progress}
                and task.due_at is not None
                and task.due_at.date() == target_date
            ]
        if status_filter == "done":
            tasks = [task for task in tasks if task.status == TaskStatus.done]
        if client_name:
            normalized_client = _normalize(client_name)
            tasks = [t for t in tasks if t.client_name and _client_matches(normalized_client, _normalize(t.client_name))]
        if status_filter == "done":
            return sorted(tasks, key=lambda t: getattr(t, "completed_at", None) or datetime.min, reverse=True)
        return sorted(tasks, key=lambda task: task.due_at or datetime.max)

    def list_delegated_tasks(
        self,
        company_id: str,
        created_by: str,
        status_filter: str = "pending",
        client_name: str | None = None,
        target_date: date | None = None,
    ) -> list[Task]:
        tasks = [
            task
            for task in self.tasks.values()
            if str(task.company_id) == company_id
            and str(task.created_by) == created_by
            and str(task.assigned_to) != created_by
        ]
        if status_filter in {"pending", "open"}:
            tasks = [task for task in tasks if task.status in {TaskStatus.pending, TaskStatus.in_progress}]
        elif status_filter == "today":
            today_start = datetime.combine(datetime.now().date(), time.min)
            today_end = datetime.combine(datetime.now().date(), time.max)
            tasks = [
                task
                for task in tasks
                if task.status in {TaskStatus.pending, TaskStatus.in_progress}
                and task.due_at is not None
                and today_start <= _as_naive_datetime(task.due_at) <= today_end
            ]
        elif status_filter == "overdue":
            now = datetime.now()
            tasks = [
                task for task in tasks
                if task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}
                and task.due_at is not None
                and _as_naive_datetime(task.due_at) < now
            ]
        elif status_filter == "date" and target_date is not None:
            tasks = [
                task
                for task in tasks
                if task.status in {TaskStatus.pending, TaskStatus.in_progress}
                and task.due_at is not None
                and _as_naive_datetime(task.due_at).date() == target_date
            ]
        elif status_filter == "done":
            tasks = [task for task in tasks if task.status == TaskStatus.done]
        if client_name:
            normalized_client = _normalize(client_name)
            tasks = [t for t in tasks if t.client_name and _client_matches(normalized_client, _normalize(t.client_name))]
        if status_filter == "done":
            return sorted(tasks, key=lambda t: getattr(t, "completed_at", None) or datetime.min, reverse=True)
        return sorted(tasks, key=lambda task: task.due_at or datetime.max)

    def list_visible_tasks(
        self,
        company_id: str,
        requester_user_id: str,
        requester_role: str = "member",
        status_filter: str = "open",
    ) -> list[Task]:
        tasks = [task for task in self.tasks.values() if str(task.company_id) == company_id]
        if requester_role not in {"owner", "admin", "manager"}:
            tasks = [
                task
                for task in tasks
                if str(task.assigned_to) == requester_user_id or str(task.created_by) == requester_user_id
            ]

        if status_filter == "open":
            tasks = [task for task in tasks if task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}]
        elif status_filter == "done":
            tasks = [task for task in tasks if task.status == TaskStatus.done]

        return sorted(tasks, key=lambda task: (task.due_at or datetime.max, task.title))

    def complete_task(self, company_id: str, user_id: str, task_reference: str) -> Task | list[Task] | None:
        matches = self._find_tasks(company_id, user_id, task_reference)
        if not matches:
            return None
        if len(matches) > 1:
            return matches
        task = matches[0]
        task.status = TaskStatus.done
        task.completed_at = datetime.now()
        self.tasks[task.id] = task
        return task

    def complete_task_by_id(self, company_id: str, user_id: str, task_id: str) -> Task | None:
        try:
            task = self.tasks[UUID(task_id)]
        except (KeyError, ValueError):
            return None

        visible_to_user = str(task.assigned_to) == user_id or str(task.created_by) == user_id
        open_status = task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}
        if str(task.company_id) != company_id or not visible_to_user or not open_status:
            return None

        task.status = TaskStatus.done
        task.completed_at = datetime.now()
        self.tasks[task.id] = task
        return task

    def start_task(self, company_id: str, user_id: str, task_reference: str) -> Task | list[Task] | None:
        matches = self._find_tasks(company_id, user_id, task_reference)
        if not matches:
            return None
        if len(matches) > 1:
            return matches
        task = matches[0]
        task.status = TaskStatus.in_progress
        self.tasks[task.id] = task
        return task

    def start_task_by_id(self, company_id: str, user_id: str, task_id: str) -> Task | None:
        try:
            task = self.tasks[UUID(task_id)]
        except (KeyError, ValueError):
            return None

        visible_to_user = str(task.assigned_to) == user_id or str(task.created_by) == user_id
        open_status = task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}
        if str(task.company_id) != company_id or not visible_to_user or not open_status:
            return None

        task.status = TaskStatus.in_progress
        self.tasks[task.id] = task
        return task

    def reschedule_task(
        self,
        company_id: str,
        user_id: str,
        task_reference: str,
        due_at: datetime,
    ) -> Task | list[Task] | None:
        matches = self._find_tasks(company_id, user_id, task_reference)
        if not matches:
            return None
        if len(matches) > 1:
            return matches
        task = matches[0]
        task.due_at = due_at
        self.tasks[task.id] = task
        return task

    def reschedule_task_by_id(self, company_id: str, user_id: str, task_id: str, due_at: datetime) -> Task | None:
        try:
            task = self.tasks[UUID(task_id)]
        except (KeyError, ValueError):
            return None

        visible_to_user = str(task.assigned_to) == user_id or str(task.created_by) == user_id
        open_status = task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}
        if str(task.company_id) != company_id or not visible_to_user or not open_status:
            return None

        task.due_at = due_at
        self.tasks[task.id] = task
        return task

    def cancel_task(self, company_id: str, user_id: str, task_reference: str) -> "Task | list[Task] | None | str":
        matches = self._find_created_tasks(company_id, user_id, task_reference)
        if matches == "permission_denied":
            return "permission_denied"
        if not matches:
            return None
        if len(matches) > 1:
            return matches
        task = matches[0]
        task.status = TaskStatus.cancelled
        self.tasks[task.id] = task
        return task

    def cancel_task_by_id(self, company_id: str, user_id: str, task_id: str) -> "Task | None | str":
        try:
            task = self.tasks[UUID(task_id)]
        except (KeyError, ValueError):
            return None
        if str(task.company_id) != company_id:
            return None
        if str(task.created_by) != user_id:
            return "permission_denied"
        if task.status not in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}:
            return None
        task.status = TaskStatus.cancelled
        self.tasks[task.id] = task
        return task

    def clear_task_due_date(self, company_id: str, user_id: str, task_reference: str) -> "Task | list[Task] | None | str":
        matches = self._find_created_tasks(company_id, user_id, task_reference)
        if matches == "permission_denied":
            return "permission_denied"
        if not matches:
            return None
        if len(matches) > 1:
            return matches
        task = matches[0]
        task.due_at = None
        self.tasks[task.id] = task
        return task

    def clear_task_due_date_by_id(self, company_id: str, user_id: str, task_id: str) -> "Task | None | str":
        try:
            task = self.tasks[UUID(task_id)]
        except (KeyError, ValueError):
            return None
        if str(task.company_id) != company_id:
            return None
        if str(task.created_by) != user_id:
            return "permission_denied"
        if task.status not in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}:
            return None
        task.due_at = None
        self.tasks[task.id] = task
        return task

    def edit_task(
        self,
        company_id: str,
        user_id: str,
        task_reference: str,
        new_title: str | None = None,
        new_assignee_name: str | None = None,
        new_client_name: str | None = None,
    ) -> "Task | list[Task] | None":
        matches = self._find_tasks(company_id, user_id, task_reference)
        if not matches:
            return None
        if len(matches) > 1:
            return matches
        task = matches[0]
        if new_title:
            task.title = new_title
        if new_assignee_name:
            new_assignee = self.find_user_by_name(company_id, new_assignee_name)
            if new_assignee:
                task.assigned_to = new_assignee
        if new_client_name is not None:
            client_id = self.find_or_create_client(company_id, new_client_name or None)
            task.client_id = client_id
            task.client_name = self.clients.get(client_id) if client_id else None
        self.tasks[task.id] = task
        return task

    def edit_task_by_id(
        self,
        company_id: str,
        user_id: str,
        task_id: str,
        new_title: str | None = None,
        new_assignee_name: str | None = None,
        new_client_name: str | None = None,
    ) -> "Task | None":
        try:
            task = self.tasks[UUID(task_id)]
        except (KeyError, ValueError):
            return None

        visible_to_user = str(task.assigned_to) == user_id or str(task.created_by) == user_id
        open_status = task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}
        if str(task.company_id) != company_id or not visible_to_user or not open_status:
            return None

        if new_title:
            task.title = new_title
        if new_assignee_name:
            new_assignee = self.find_user_by_name(company_id, new_assignee_name)
            if new_assignee:
                task.assigned_to = new_assignee
        if new_client_name is not None:
            client_id = self.find_or_create_client(company_id, new_client_name or None)
            task.client_id = client_id
            task.client_name = self.clients.get(client_id) if client_id else None
        self.tasks[task.id] = task
        return task

    def save_pending_choice(
        self,
        company_id: str,
        user_id: str,
        action: str,
        matches: list[dict],
        params: dict | None = None,
        ttl_minutes: int = 10,
    ) -> None:
        self.pending_choices[(company_id, user_id)] = {
            "action": action,
            "matches": matches,
            "params": params or {},
            "expires_at": datetime.now() + timedelta(minutes=ttl_minutes),
        }

    def get_pending_choice(self, company_id: str, user_id: str) -> dict | None:
        pending = self.pending_choices.get((company_id, user_id))
        if not pending:
            return None
        if pending["expires_at"] < datetime.now():
            self.clear_pending_choice(company_id, user_id)
            return None
        return pending

    def clear_pending_choice(self, company_id: str, user_id: str) -> None:
        self.pending_choices.pop((company_id, user_id), None)

    def save_pending_task_draft(
        self,
        company_id: str,
        user_id: str,
        params: dict,
        ttl_minutes: int = 30,
    ) -> None:
        self.pending_task_drafts[(company_id, user_id)] = {
            "params": params,
            "expires_at": datetime.now() + timedelta(minutes=ttl_minutes),
        }

    def get_pending_task_draft(self, company_id: str, user_id: str) -> dict | None:
        draft = self.pending_task_drafts.get((company_id, user_id))
        if not draft:
            return None
        if draft["expires_at"] < datetime.now():
            self.clear_pending_task_draft(company_id, user_id)
            return None
        return draft

    def clear_pending_task_draft(self, company_id: str, user_id: str) -> None:
        self.pending_task_drafts.pop((company_id, user_id), None)


    def save_pending_invite_draft(
        self,
        company_id: str,
        user_id: str,
        params: dict,
        ttl_minutes: int = 24 * 60,
    ) -> None:
        self.pending_invite_drafts[(company_id, user_id)] = {
            "params": params,
            "expires_at": datetime.now() + timedelta(minutes=ttl_minutes),
        }

    def get_pending_invite_draft(self, company_id: str, user_id: str) -> dict | None:
        draft = self.pending_invite_drafts.get((company_id, user_id))
        if not draft:
            return None
        if draft["expires_at"] < datetime.now():
            self.clear_pending_invite_draft(company_id, user_id)
            return None
        return draft

    def clear_pending_invite_draft(self, company_id: str, user_id: str) -> None:
        self.pending_invite_drafts.pop((company_id, user_id), None)
    def get_onboarding_session(self, phone: str) -> dict | None:
        for variant in phone_variants(phone):
            session = self.onboarding_sessions.get(variant)
            if session:
                return session
        return None

    def start_onboarding_session(self, phone: str) -> dict:
        phone = normalize_phone(phone)
        session = {
            "phone": phone,
            "step": "company_name",
            "company_name": None,
            "user_name": None,
            "job_title": None,
        }
        self.onboarding_sessions[phone] = session
        return session

    def update_onboarding_session(self, phone: str, **updates) -> dict:
        phone = normalize_phone(phone)
        session = self.onboarding_sessions[phone]
        session.update(updates)
        return session

    def clear_onboarding_session(self, phone: str) -> None:
        for variant in phone_variants(phone):
            self.onboarding_sessions.pop(variant, None)

    def complete_onboarding_session(self, phone: str) -> dict[str, str]:
        phone = normalize_phone(phone)
        session = self.onboarding_sessions.pop(phone)
        self.company_id = uuid4()
        self.company_name = session["company_name"]
        self.owner_id = uuid4()
        self.users = {
            self.owner_id: session["user_name"],
        }
        self.user_job_titles = {
            self.owner_id: session["job_title"],
        }
        self.user_roles = {
            self.owner_id: "owner",
        }
        self.user_phones = {
            self.owner_id: phone,
        }
        self.phone_to_user = {
            phone: self.owner_id,
        }
        self.clients = {}
        return {
            "company_id": str(self.company_id),
            "company_name": self.company_name,
            "user_id": str(self.owner_id),
            "user_name": session["user_name"],
            "job_title": session["job_title"],
            "role": "owner",
        }

    def create_invite(
        self,
        company_id: str,
        invited_by: str,
        phone: str,
        name: str | None = None,
        job_title: str | None = None,
        role: str = "member",
        expires_at: datetime | None = None,
    ) -> dict:
        phone = normalize_phone(phone)
        for invite in self.invites.values():
            if (
                invite["company_id"] == company_id
                and invite["phone"] in phone_variants(phone)
                and invite["status"] == InviteStatus.pending.value
            ):
                invite.update(
                    {
                        "invited_by": invited_by,
                        "name": name,
                        "job_title": job_title,
                        "role": role,
                        "expires_at": expires_at,
                    }
                )
                return invite

        invite_id = uuid4()
        invite = {
            "id": str(invite_id),
            "company_id": company_id,
            "invited_by": invited_by,
            "phone": phone,
            "name": name,
            "job_title": job_title,
            "role": role,
            "status": InviteStatus.pending.value,
            "accepted_at": None,
            "declined_at": None,
            "expires_at": expires_at,
            "company_name": self.company_name,
            "invited_by_name": self.users.get(UUID(invited_by)),
        }
        self.invites[invite_id] = invite
        return invite

    def get_pending_invite_by_phone(self, phone: str) -> dict | None:
        variants = set(phone_variants(phone))
        pending = [
            invite
            for invite in self.invites.values()
            if invite["phone"] in variants and invite["status"] == InviteStatus.pending.value
        ]
        if not pending:
            return None
        return pending[-1]

    def mark_invite_status(self, invite_id: str, status: str, timestamp: datetime) -> dict | None:
        invite = self.invites.get(UUID(invite_id))
        if not invite:
            return None
        invite["status"] = status
        if status == InviteStatus.accepted.value:
            invite["accepted_at"] = timestamp
        if status == InviteStatus.declined.value:
            invite["declined_at"] = timestamp
        return invite

    def accept_invite(self, invite_id: str, phone: str) -> dict | None:
        phone = normalize_phone(phone)
        invite = self.invites.get(UUID(invite_id))
        if not invite or invite["phone"] not in phone_variants(phone) or invite["status"] != InviteStatus.pending.value:
            return None

        user_id = None
        for variant in phone_variants(phone):
            user_id = self.phone_to_user.get(variant)
            if user_id is not None:
                break
        user_id = user_id or uuid4()
        self.users[user_id] = invite["name"] or "Usuario"
        self.user_job_titles[user_id] = invite["job_title"]
        self.user_roles[user_id] = invite["role"]
        self.user_phones[user_id] = phone
        self.phone_to_user[phone] = user_id
        for variant in phone_variants(phone):
            self.phone_to_user[variant] = user_id

        now = datetime.now()
        invite["status"] = InviteStatus.accepted.value
        invite["accepted_at"] = now
        self.clear_onboarding_session(phone)

        return {
            "company_id": invite["company_id"],
            "company_name": invite.get("company_name") or self.company_name,
            "user_id": str(user_id),
            "user_name": self.users[user_id],
            "job_title": invite["job_title"],
            "role": invite["role"],
        }

    def decline_invite(self, invite_id: str) -> dict | None:
        return self.mark_invite_status(invite_id, InviteStatus.declined.value, datetime.now())

    def enqueue_notification(
        self,
        company_id: str,
        task_id: str | None,
        recipient_user_id: str | None,
        recipient_phone: str,
        notification_type: str,
        message: str,
    ) -> dict:
        notification_id = uuid4()
        notification = {
            "id": str(notification_id),
            "company_id": company_id,
            "task_id": task_id,
            "recipient_user_id": recipient_user_id,
            "recipient_phone": recipient_phone,
            "notification_type": notification_type,
            "message": message,
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": datetime.now(),
            "sent_at": None,
        }
        self.notification_outbox[notification_id] = notification
        return notification

    def list_due_notifications(self, now: datetime, limit: int = 20) -> list[dict]:
        due = [
            notification
            for notification in self.notification_outbox.values()
            if notification["status"] == "pending" and notification["next_attempt_at"] <= now
        ]
        return sorted(due, key=lambda item: item["next_attempt_at"])[:limit]

    def mark_notification_sent(self, notification_id: str, sent_at: datetime, provider_response: dict | None = None) -> None:
        notification = self.notification_outbox.get(UUID(notification_id))
        if not notification:
            return
        notification["status"] = "sent"
        notification["sent_at"] = sent_at
        notification["provider_response"] = provider_response or {}

    def mark_notification_failed(
        self,
        notification_id: str,
        error: str,
        failed_at: datetime,
        max_attempts: int = 5,
    ) -> bool:
        notification = self.notification_outbox.get(UUID(notification_id))
        if not notification:
            return False
        notification["attempts"] += 1
        notification["last_error"] = error
        if notification["attempts"] >= max_attempts:
            notification["status"] = "failed"
            notification["next_attempt_at"] = failed_at
            return True
        delay_minutes = min(30, 2 ** notification["attempts"])
        notification["next_attempt_at"] = failed_at + timedelta(minutes=delay_minutes)
        return False

    def count_failed_notifications(self, since: datetime) -> int:
        return sum(
            1 for n in self.notification_outbox.values()
            if n["status"] == "failed"
            and n.get("next_attempt_at", datetime.min) >= since
        )

    def team_summary(self, company_id: str, member_name: str | None = None) -> dict:
        company_tasks = [task for task in self.tasks.values() if str(task.company_id) == company_id]
        if member_name:
            member_id = None
            normalized_member = _normalize(member_name)
            for user_id, user_name in self.users.items():
                if _normalize(user_name) == normalized_member:
                    member_id = user_id
                    break
            if member_id is None:
                return {
                    "pending": 0,
                    "done": 0,
                    "overdue": 0,
                    "pending_tasks": [],
                    "overdue_tasks": [],
                    "done_today_tasks": [],
                }
            company_tasks = [task for task in company_tasks if task.assigned_to == member_id]
        now = datetime.now()
        pending_tasks = [
            task
            for task in company_tasks
            if task.status in {TaskStatus.pending, TaskStatus.in_progress}
            and (task.due_at is None or _as_naive_datetime(task.due_at) >= now)
        ]
        overdue_tasks = [
            task
            for task in company_tasks
            if task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}
            and task.due_at is not None
            and _as_naive_datetime(task.due_at) < now
        ]
        done_tasks = [task for task in company_tasks if task.status == TaskStatus.done]
        return {
            "pending": len(pending_tasks),
            "done": len(done_tasks),
            "overdue": len(overdue_tasks),
            "pending_tasks": [self._team_task_summary(task) for task in sorted(pending_tasks, key=lambda item: item.due_at or datetime.max)[:5]],
            "overdue_tasks": [self._team_task_summary(task) for task in sorted(overdue_tasks, key=lambda item: item.due_at or datetime.max)[:5]],
            "done_today_tasks": [
                self._team_task_summary(task)
                for task in done_tasks
                if getattr(task, "completed_at", None) and task.completed_at.date() == now.date()
            ][:5],
        }

    def _team_task_summary(self, task: Task) -> dict:
        return {
            "id": str(task.id),
            "title": task.title,
            "assignee_name": self.users.get(task.assigned_to, "Usuario"),
            "client_name": task.client_name,
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "completed_at": getattr(task, "completed_at", None).isoformat()
            if getattr(task, "completed_at", None)
            else None,
            "status": task.status.value,
        }

    def get_message_by_provider_id(self, provider_message_id: str) -> str | None:
        for m in self.messages:
            if m.get("provider_message_id") == provider_message_id:
                return m.get("response_body")
        return None

    def get_recent_messages(self, company_id: str, user_id: str, limit: int = 10) -> list[dict]:
        history = []
        relevant = [
            m for m in self.messages
            if m["company_id"] == company_id and m["user_id"] == user_id
        ][-limit:]
        for m in relevant:
            history.append({"direction": "inbound", "body": m["body"]})
            if m.get("response_body"):
                history.append({"direction": "outbound", "body": m["response_body"]})
        return history

    def log_whatsapp_message(
        self,
        company_id: str,
        user_id: str,
        provider_message_id: str | None,
        body: str,
        parsed: dict,
        response_body: str,
        error: str | None = None,
        from_phone: str = "",
    ) -> None:
        self.messages.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "company_id": company_id,
                "user_id": user_id,
                "from_phone": from_phone,
                "provider_message_id": provider_message_id,
                "body": body,
                "parsed": parsed,
                "response_body": response_body,
                "error": error,
            }
        )

    def get_observe_messages(self, limit: int = 50) -> list[dict]:
        return list(reversed(self.messages[-limit:]))

    def list_reminder_candidates(
        self,
        now: datetime,
        window_minutes: int = 60,
    ) -> list[ReminderCandidate]:
        candidates = []
        for task in self.tasks.values():
            if task.status not in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}:
                continue
            if task.due_at is None:
                continue

            kind = _reminder_kind(task.due_at, now, window_minutes, settings.overdue_reminder_max_age_days)
            if kind is None or (task.id, kind) in self.sent_reminders:
                continue

            candidates.append(
                ReminderCandidate(
                    task_id=task.id,
                    company_id=task.company_id,
                    title=task.title,
                    assigned_to=task.assigned_to,
                    assignee_name=self.users.get(task.assigned_to, "Usuario"),
                    assignee_phone=self.user_phones.get(task.assigned_to, ""),
                    due_at=task.due_at,
                    kind=kind,
                )
            )

        return candidates

    def claim_reminder_candidates(self, candidates: list[ReminderCandidate]) -> list[ReminderCandidate]:
        # In-memory store has no concurrency — all candidates are claimable.
        return [c for c in candidates if (c.task_id, c.kind) not in self.sent_reminders]

    def mark_reminder_sent(self, candidate: ReminderCandidate, sent_at: datetime) -> None:
        self.sent_reminders.add((candidate.task_id, candidate.kind))

    def _find_created_tasks(self, company_id: str, user_id: str, task_reference: str) -> "list[Task] | str":
        """Like _find_tasks but restricted to tasks created by user_id.
        Returns 'permission_denied' when a visible-but-not-owned task is the only match.
        """
        owned = [
            task for task in self.tasks.values()
            if str(task.company_id) == company_id
            and str(task.created_by) == user_id
            and task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}
        ]
        def _score_owned(task: Task) -> float:
            title_score = _fuzzy_match_score(task_reference, task.title)
            if task.client_name:
                return max(title_score, _fuzzy_match_score(task_reference, f"{task.title} {task.client_name}"))
            return title_score

        scored = [
            (task, s)
            for task in owned
            for s in [_score_owned(task)]
            if s >= 0.65 or str(task.id).startswith(task_reference)
        ]
        if scored:
            scored.sort(key=lambda x: (-x[1], x[0].due_at or datetime.max))
            return [task for task, _ in scored[:5]]

        # Nothing matched among owned tasks — check if the reference matches a
        # visible-but-not-owned task so we can return a clear permission error.
        all_visible = self._find_tasks(company_id, user_id, task_reference)
        if all_visible:
            return "permission_denied"
        return []

    def _find_tasks(self, company_id: str, user_id: str, task_reference: str) -> list[Task]:
        candidates = [
            task for task in self.tasks.values()
            if str(task.company_id) == company_id
            and (str(task.assigned_to) == user_id or str(task.created_by) == user_id)
            and task.status in {TaskStatus.pending, TaskStatus.in_progress, TaskStatus.overdue}
        ]
        def _score(task: Task) -> float:
            title_score = _fuzzy_match_score(task_reference, task.title)
            if task.client_name:
                combined = f"{task.title} {task.client_name}"
                return max(title_score, _fuzzy_match_score(task_reference, combined))
            return title_score

        scored = [
            (task, s)
            for task in candidates
            for s in [_score(task)]
            if s >= 0.65 or str(task.id).startswith(task_reference)
        ]
        scored.sort(key=lambda x: (-x[1], x[0].due_at or datetime.max))
        return [task for task, _ in scored[:5]]


def _normalize(value: str) -> str:
    text = normalize("NFKD", value.strip().lower())
    return "".join(char for char in text if char.isascii())


def _fuzzy_match_score(reference: str, title: str) -> float:
    ref = _normalize(reference)
    ttl = _normalize(title)

    if ref in ttl:
        return 1.0

    ref_words = ref.split()
    ttl_words = ttl.split()
    if not ref_words or not ttl_words:
        return 0.0

    total = sum(
        max((SequenceMatcher(None, rw, tw).ratio() for tw in ttl_words), default=0.0)
        for rw in ref_words
    )
    return total / len(ref_words)


def _is_likely_same_person_name(candidate: str, existing: str) -> bool:
    if len(candidate) < 4 or len(existing) < 4:
        return False
    return candidate.startswith(existing) or existing.startswith(candidate)


def _client_matches(needle: str, haystack: str) -> bool:
    if needle in haystack or haystack.startswith(needle):
        return True
    return SequenceMatcher(None, needle, haystack).ratio() >= 0.75


def _as_naive_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.replace(tzinfo=None)


def _reminder_kind(
    due_at: datetime,
    now: datetime,
    window_minutes: int,
    overdue_max_age_days: int = 7,
) -> ReminderKind | None:
    seconds_until_due = (due_at - now).total_seconds()
    if seconds_until_due < 0:
        if abs(seconds_until_due) > overdue_max_age_days * 86400:
            return None
        return ReminderKind.overdue
    if seconds_until_due <= 5 * 60:
        return ReminderKind.due_now
    if seconds_until_due <= window_minutes * 60:
        return ReminderKind.due_soon
    return None


def build_store():
    if settings.database_url:
        from whatsapp_task_agent.postgres_store import PostgresTaskStore

        return PostgresTaskStore(settings.database_url)
    return InMemoryTaskStore()


store = build_store()
