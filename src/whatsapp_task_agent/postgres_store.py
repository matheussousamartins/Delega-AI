from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from unicodedata import normalize

from whatsapp_task_agent.phone import normalize_phone, phone_variants
from whatsapp_task_agent.schemas import InviteStatus, Priority, ReminderCandidate, ReminderKind, Task, TaskStatus
from whatsapp_task_agent.settings import settings


class PostgresTaskStore:
    def __init__(self, database_url: str) -> None:
        self._pool = ConnectionPool(
            database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    def identify_user(self, from_phone: str) -> dict[str, str]:
        phones = phone_variants(from_phone)
        with self._connect() as conn:
            row = conn.execute(
                """
                select
                    c.id as company_id,
                    c.name as company_name,
                    u.id as user_id,
                    u.name as user_name,
                    cm.role
                from users u
                join company_members cm on cm.user_id = u.id
                join companies c on c.id = cm.company_id
                where u.phone = any(%(phones)s)
                order by cm.created_at asc
                limit 1
                """,
                {"phones": phones},
            ).fetchone()

        if row is None:
            raise ValueError("phone_not_registered")

        return {
            "company_id": str(row["company_id"]),
            "company_name": row["company_name"],
            "user_id": str(row["user_id"]),
            "user_name": row["user_name"],
            "role": row["role"],
        }

    def find_user_by_name(self, company_id: str, name: str | None) -> UUID | None:
        if not name or name == "self":
            return None

        candidate = _normalize_name(name)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select u.id, u.name
                from users u
                join company_members cm on cm.user_id = u.id
                where cm.company_id = %(company_id)s
                order by u.name asc
                """,
                {"company_id": company_id},
            ).fetchall()

        exact_matches = [row["id"] for row in rows if _normalize_name(row["name"]) == candidate]
        if len(exact_matches) > 1:
            return None  # ambiguous — let find_assignee_suggestions handle disambiguation
        if len(exact_matches) == 1:
            return exact_matches[0]
        for row in rows:
            existing = _normalize_name(row["name"])
            if _is_likely_same_person_name(candidate, existing):
                return row["id"]
        return None

    def get_user_name(self, user_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "select name from users where id = %(user_id)s limit 1",
                {"user_id": user_id},
            ).fetchone()
        return row["name"] if row else None

    def get_user_phone(self, user_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "select phone from users where id = %(user_id)s limit 1",
                {"user_id": user_id},
            ).fetchone()
        return row["phone"] if row else None

    def update_member_name(self, company_id: str, target_user_id: str, new_name: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                """
                update users set name = %(new_name)s
                where id = %(user_id)s
                  and exists (
                      select 1 from company_members
                      where company_id = %(company_id)s and user_id = %(user_id)s
                  )
                """,
                {"new_name": new_name, "user_id": target_user_id, "company_id": company_id},
            )
            conn.commit()
        return (result.rowcount or 0) > 0

    def list_company_members(self, company_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select u.id, u.name, u.phone, u.job_title, cm.role
                from users u
                join company_members cm on cm.user_id = u.id
                where cm.company_id = %(company_id)s
                order by u.name asc
                """,
                {"company_id": company_id},
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "phone": row["phone"],
                "job_title": row["job_title"],
                "role": row["role"],
            }
            for row in rows
        ]

    def list_clients(self, company_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, name
                from clients
                where company_id = %(company_id)s
                order by name asc
                """,
                {"company_id": company_id},
            ).fetchall()
        return [{"id": str(row["id"]), "name": row["name"]} for row in rows]
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
        assigned_to = self.find_user_by_name(company_id, assignee_name) or UUID(created_by)
        with self._connect() as conn:
            client = self._find_or_create_client(conn, company_id, client_name)
            row = conn.execute(
                """
                insert into tasks (
                    company_id, client_id, title, description, priority, assigned_to, created_by, due_at
                )
                values (
                    %(company_id)s, %(client_id)s, %(title)s, %(description)s, %(priority)s,
                    %(assigned_to)s, %(created_by)s, %(due_at)s
                )
                returning id, company_id, client_id, title, assigned_to, created_by, status, priority,
                    due_at, completed_at, description
                """,
                {
                    "company_id": company_id,
                    "client_id": client["id"] if client else None,
                    "title": title,
                    "description": description,
                    "priority": priority,
                    "assigned_to": assigned_to,
                    "created_by": created_by,
                    "due_at": due_at,
                },
            ).fetchone()
            conn.execute(
                """
                insert into task_events (task_id, actor_id, event_type, payload)
                values (%(task_id)s, %(actor_id)s, 'created', %(payload)s)
                """,
                {"task_id": row["id"], "actor_id": created_by, "payload": Jsonb({"source": "whatsapp"})},
            )
            conn.commit()
        task = _task_from_row(row)
        if client:
            task.client_name = client["name"]
        return task

    def _find_or_create_client(self, conn: Connection, company_id: str, client_name: str | None) -> dict[str, Any] | None:
        if not client_name:
            return None
        row = conn.execute(
            """
            insert into clients (company_id, name, normalized_name)
            values (%(company_id)s, %(name)s, lower(%(name)s))
            on conflict (company_id, normalized_name)
            do update set name = excluded.name
            returning id, name
            """,
            {"company_id": company_id, "name": client_name},
        ).fetchone()
        return dict(row) if row else None

    def list_tasks(
        self,
        company_id: str,
        assigned_to: str,
        status_filter: str = "pending",
        target_date: date | None = None,
        client_name: str | None = None,
    ) -> list[Task]:
        clauses = ["company_id = %(company_id)s", "assigned_to = %(assigned_to)s"]
        params: dict[str, Any] = {"company_id": company_id, "assigned_to": assigned_to}

        if status_filter == "pending":
            clauses.append("status in ('pending', 'in_progress')")
        elif status_filter == "today":
            clauses.append("status in ('pending', 'in_progress')")
            clauses.append("due_at >= date_trunc('day', now())")
            clauses.append("due_at < date_trunc('day', now()) + interval '1 day'")
        elif status_filter == "overdue":
            clauses.append("status in ('pending', 'in_progress', 'overdue')")
            clauses.append("due_at < now()")
        elif status_filter == "date" and target_date is not None:
            clauses.append("status in ('pending', 'in_progress')")
            clauses.append("due_at >= %(target_date)s::date")
            clauses.append("due_at < %(target_date)s::date + interval '1 day'")
            params["target_date"] = target_date
        elif status_filter == "done":
            clauses.append("status = 'done'")

        if client_name:
            clauses.append("c.name ilike %(client_name_pattern)s")
            params["client_name_pattern"] = f"%{client_name}%"

        order_clause = "t.completed_at desc nulls last, t.created_at desc" if status_filter == "done" else "t.due_at asc nulls last, t.created_at asc"
        row_limit = 100 if status_filter == "done" else 50

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select t.id, t.company_id, t.client_id, c.name as client_name, t.title,
                    t.assigned_to, t.created_by, t.status, t.priority, t.due_at,
                    t.completed_at, t.description
                from tasks t
                left join clients c on c.id = t.client_id
                where {" and ".join('t.' + clause if clause.startswith(('company_id', 'assigned_to', 'status', 'due_at')) else clause for clause in clauses)}
                order by {order_clause}
                limit {row_limit}
                """,
                params,
            ).fetchall()

        return [_task_from_row(row) for row in rows]

    def list_delegated_tasks(
        self,
        company_id: str,
        created_by: str,
        status_filter: str = "pending",
        client_name: str | None = None,
    ) -> list[Task]:
        clauses = [
            "t.company_id = %(company_id)s",
            "t.created_by = %(created_by)s",
            "t.assigned_to != %(created_by)s",
        ]
        params: dict[str, Any] = {"company_id": company_id, "created_by": created_by}

        if status_filter in {"pending", "open"}:
            clauses.append("t.status in ('pending', 'in_progress')")
        elif status_filter == "overdue":
            clauses.append("t.status in ('pending', 'in_progress', 'overdue')")
            clauses.append("t.due_at < now()")
        elif status_filter == "done":
            clauses.append("t.status = 'done'")

        if client_name:
            clauses.append("c.name ilike %(client_name_pattern)s")
            params["client_name_pattern"] = f"%{client_name}%"

        order_clause = "t.completed_at desc nulls last, t.created_at desc" if status_filter == "done" else "t.due_at asc nulls last, t.created_at asc"
        row_limit = 100 if status_filter == "done" else 50

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select t.id, t.company_id, t.client_id, c.name as client_name, t.title,
                    t.assigned_to, t.created_by, t.status, t.priority, t.due_at,
                    t.completed_at, t.description
                from tasks t
                left join clients c on c.id = t.client_id
                where {" and ".join(clauses)}
                order by {order_clause}
                limit {row_limit}
                """,
                params,
            ).fetchall()

        return [_task_from_row(row) for row in rows]

    def list_visible_tasks(
        self,
        company_id: str,
        requester_user_id: str,
        requester_role: str = "member",
        status_filter: str = "open",
    ) -> list[Task]:
        visibility_clause = ""
        if requester_role not in {"owner", "admin", "manager"}:
            visibility_clause = "and (t.assigned_to = %(requester_user_id)s or t.created_by = %(requester_user_id)s)"

        status_clause = ""
        if status_filter == "open":
            status_clause = "and t.status in ('pending', 'in_progress', 'overdue')"
        elif status_filter == "done":
            status_clause = "and t.status = 'done'"

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select t.id, t.company_id, t.client_id, c.name as client_name, t.title,
                    t.assigned_to, t.created_by, t.status, t.priority, t.due_at,
                    t.completed_at, t.description
                from tasks t
                left join clients c on c.id = t.client_id
                where t.company_id = %(company_id)s
                  {visibility_clause}
                  {status_clause}
                order by t.due_at asc nulls last, t.created_at asc
                limit 100
                """,
                {
                    "company_id": company_id,
                    "requester_user_id": requester_user_id,
                },
            ).fetchall()

        return [_task_from_row(row) for row in rows]

    def complete_task(self, company_id: str, user_id: str, task_reference: str) -> Task | list[Task] | None:
        return self._update_referenced_task(
            company_id=company_id,
            user_id=user_id,
            task_reference=task_reference,
            updates="status = 'done', completed_at = now(), updated_at = now()",
            event_type="completed",
        )

    def complete_task_by_id(self, company_id: str, user_id: str, task_id: str) -> Task | None:
        return self._update_task_by_id(
            company_id=company_id,
            user_id=user_id,
            task_id=task_id,
            updates="status = 'done', completed_at = now(), updated_at = now()",
            event_type="completed",
        )

    def start_task(self, company_id: str, user_id: str, task_reference: str) -> Task | list[Task] | None:
        return self._update_referenced_task(
            company_id=company_id,
            user_id=user_id,
            task_reference=task_reference,
            updates="status = 'in_progress', updated_at = now()",
            event_type="started",
        )

    def start_task_by_id(self, company_id: str, user_id: str, task_id: str) -> Task | None:
        return self._update_task_by_id(
            company_id=company_id,
            user_id=user_id,
            task_id=task_id,
            updates="status = 'in_progress', updated_at = now()",
            event_type="started",
        )

    def reschedule_task(
        self,
        company_id: str,
        user_id: str,
        task_reference: str,
        due_at: datetime,
    ) -> Task | list[Task] | None:
        return self._update_referenced_task(
            company_id=company_id,
            user_id=user_id,
            task_reference=task_reference,
            updates="due_at = %(due_at)s, updated_at = now()",
            event_type="rescheduled",
            extra_params={"due_at": due_at},
        )

    def reschedule_task_by_id(self, company_id: str, user_id: str, task_id: str, due_at: datetime) -> Task | None:
        return self._update_task_by_id(
            company_id=company_id,
            user_id=user_id,
            task_id=task_id,
            updates="due_at = %(due_at)s, updated_at = now()",
            event_type="rescheduled",
            extra_params={"due_at": due_at},
        )

    def cancel_task(self, company_id: str, user_id: str, task_reference: str) -> "Task | list[Task] | None | str":
        candidates = self._find_task_candidates(company_id, user_id, task_reference)
        if not candidates:
            all_visible = self._find_task_candidates_visible(company_id, user_id, task_reference)
            if all_visible:
                return "permission_denied"
            return None
        owned = [t for t in candidates if str(t.created_by) == user_id]
        if not owned:
            return "permission_denied"
        if len(owned) > 1:
            return owned
        return self._apply_task_cancel(company_id, user_id, str(owned[0].id))

    def cancel_task_by_id(self, company_id: str, user_id: str, task_id: str) -> "Task | None | str":
        with self._connect() as conn:
            row = conn.execute(
                """
                select id, created_by, status, company_id
                from tasks
                where id = %(task_id)s and company_id = %(company_id)s
                limit 1
                """,
                {"task_id": task_id, "company_id": company_id},
            ).fetchone()
        if not row:
            return None
        if str(row["created_by"]) != user_id:
            return "permission_denied"
        if row["status"] not in ("pending", "in_progress", "overdue"):
            return None
        return self._apply_task_cancel(company_id, user_id, task_id)

    def _apply_task_cancel(self, company_id: str, user_id: str, task_id: str) -> "Task | None":
        with self._connect() as conn:
            row = conn.execute(
                """
                update tasks
                set status = 'cancelled', updated_at = now()
                where id = %(task_id)s
                  and company_id = %(company_id)s
                  and created_by = %(user_id)s
                  and status in ('pending', 'in_progress', 'overdue')
                returning id, company_id, client_id, title, assigned_to, created_by,
                    status, priority, due_at, completed_at, description
                """,
                {"task_id": task_id, "company_id": company_id, "user_id": user_id},
            ).fetchone()
            if row:
                conn.execute(
                    """
                    insert into task_events (task_id, actor_id, event_type, payload)
                    values (%(task_id)s, %(user_id)s, 'cancelled', %(payload)s)
                    """,
                    {"task_id": row["id"], "user_id": user_id, "payload": Jsonb({"source": "whatsapp"})},
                )
                conn.commit()
        return _task_from_row(row) if row else None

    def clear_task_due_date(self, company_id: str, user_id: str, task_reference: str) -> "Task | list[Task] | None | str":
        candidates = self._find_task_candidates(company_id, user_id, task_reference)
        if not candidates:
            all_visible = self._find_task_candidates_visible(company_id, user_id, task_reference)
            if all_visible:
                return "permission_denied"
            return None
        owned = [t for t in candidates if str(t.created_by) == user_id]
        if not owned:
            return "permission_denied"
        if len(owned) > 1:
            return owned
        return self._apply_clear_due_date(company_id, user_id, str(owned[0].id))

    def clear_task_due_date_by_id(self, company_id: str, user_id: str, task_id: str) -> "Task | None | str":
        with self._connect() as conn:
            row = conn.execute(
                """
                select id, created_by, status, company_id
                from tasks
                where id = %(task_id)s and company_id = %(company_id)s
                limit 1
                """,
                {"task_id": task_id, "company_id": company_id},
            ).fetchone()
        if not row:
            return None
        if str(row["created_by"]) != user_id:
            return "permission_denied"
        if row["status"] not in ("pending", "in_progress", "overdue"):
            return None
        return self._apply_clear_due_date(company_id, user_id, task_id)

    def _apply_clear_due_date(self, company_id: str, user_id: str, task_id: str) -> "Task | None":
        with self._connect() as conn:
            row = conn.execute(
                """
                update tasks
                set due_at = null, updated_at = now()
                where id = %(task_id)s
                  and company_id = %(company_id)s
                  and created_by = %(user_id)s
                  and status in ('pending', 'in_progress', 'overdue')
                returning id, company_id, client_id, title, assigned_to, created_by,
                    status, priority, due_at, completed_at, description
                """,
                {"task_id": task_id, "company_id": company_id, "user_id": user_id},
            ).fetchone()
            if row:
                conn.execute(
                    """
                    insert into task_events (task_id, actor_id, event_type, payload)
                    values (%(task_id)s, %(user_id)s, 'due_date_cleared', %(payload)s)
                    """,
                    {"task_id": row["id"], "user_id": user_id, "payload": Jsonb({"source": "whatsapp"})},
                )
                conn.commit()
        return _task_from_row(row) if row else None

    def _find_task_candidates_visible(
        self,
        company_id: str,
        user_id: str,
        task_reference: str,
    ) -> list[Task]:
        """Find tasks visible to user_id but NOT necessarily created by them."""
        return self._find_task_candidates(company_id, user_id, task_reference)

    def edit_task(
        self,
        company_id: str,
        user_id: str,
        task_reference: str,
        new_title: str | None = None,
        new_assignee_name: str | None = None,
        new_client_name: str | None = None,
    ) -> "Task | list[Task] | None":
        candidates = self._find_task_candidates(company_id, user_id, task_reference)
        if not candidates:
            return None
        if len(candidates) > 1:
            return candidates
        return self._apply_task_edit(company_id, user_id, str(candidates[0].id), new_title, new_assignee_name, new_client_name)

    def edit_task_by_id(
        self,
        company_id: str,
        user_id: str,
        task_id: str,
        new_title: str | None = None,
        new_assignee_name: str | None = None,
        new_client_name: str | None = None,
    ) -> "Task | None":
        return self._apply_task_edit(company_id, user_id, task_id, new_title, new_assignee_name, new_client_name)

    def _apply_task_edit(
        self,
        company_id: str,
        user_id: str,
        task_id: str,
        new_title: str | None,
        new_assignee_name: str | None,
        new_client_name: str | None,
    ) -> "Task | None":
        set_clauses = ["updated_at = now()"]
        params: dict[str, Any] = {"task_id": task_id, "company_id": company_id, "user_id": user_id}

        if new_title:
            set_clauses.append("title = %(new_title)s")
            params["new_title"] = new_title

        if new_assignee_name:
            new_assignee_id = self.find_user_by_name(company_id, new_assignee_name)
            if new_assignee_id:
                set_clauses.append("assigned_to = %(new_assignee_id)s")
                params["new_assignee_id"] = new_assignee_id

        if new_client_name is not None:
            if new_client_name:
                with self._connect() as conn:
                    client = self._find_or_create_client(conn, company_id, new_client_name)
                    conn.commit()
                if client:
                    set_clauses.append("client_id = %(new_client_id)s")
                    params["new_client_id"] = client["id"]
            else:
                set_clauses.append("client_id = null")

        with self._connect() as conn:
            row = conn.execute(
                f"""
                update tasks
                set {", ".join(set_clauses)}
                where id = %(task_id)s
                  and company_id = %(company_id)s
                  and (assigned_to = %(user_id)s or created_by = %(user_id)s)
                  and status in ('pending', 'in_progress', 'overdue')
                returning id, company_id, client_id, title, assigned_to, created_by, status,
                    priority, due_at, completed_at, description
                """,
                params,
            ).fetchone()
            if row:
                conn.execute(
                    """
                    insert into task_events (task_id, actor_id, event_type, payload)
                    values (%(task_id)s, %(user_id)s, 'edited', %(payload)s)
                    """,
                    {"task_id": row["id"], "user_id": user_id, "payload": Jsonb({"source": "whatsapp"})},
                )
                conn.commit()

        if not row:
            return None
        task = _task_from_row(row)
        if new_client_name:
            task.client_name = new_client_name
        return task

    def team_summary(self, company_id: str, member_name: str | None = None) -> dict[str, Any]:
        member_clause = ""
        params: dict[str, Any] = {"company_id": company_id}
        if member_name:
            member_clause = "and u.name ilike %(member_name)s"
            params["member_name"] = member_name

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select
                    case
                        when t.status = 'done' then 'done'
                        when t.due_at is not null and t.due_at < now()
                            and t.status in ('pending', 'in_progress', 'overdue')
                            then 'overdue'
                        when t.status in ('pending', 'in_progress') then 'pending'
                        else t.status::text
                    end as bucket,
                    count(*) as total
                from tasks t
                join users u on u.id = t.assigned_to
                where t.company_id = %(company_id)s
                {member_clause}
                group by bucket
                """,
                params,
            ).fetchall()
            pending_rows = conn.execute(
                f"""
                select t.id, t.title, t.status, t.due_at, t.completed_at,
                    u.name as assignee_name, c.name as client_name
                from tasks t
                join users u on u.id = t.assigned_to
                left join clients c on c.id = t.client_id
                where t.company_id = %(company_id)s
                  {member_clause}
                  and t.status in ('pending', 'in_progress')
                  and (t.due_at is null or t.due_at >= now())
                order by t.due_at asc nulls last, t.created_at asc
                limit 5
                """,
                params,
            ).fetchall()
            overdue_rows = conn.execute(
                f"""
                select t.id, t.title, t.status, t.due_at, t.completed_at,
                    u.name as assignee_name, c.name as client_name
                from tasks t
                join users u on u.id = t.assigned_to
                left join clients c on c.id = t.client_id
                where t.company_id = %(company_id)s
                  {member_clause}
                  and t.status in ('pending', 'in_progress', 'overdue')
                  and t.due_at is not null
                  and t.due_at < now()
                order by t.due_at asc, t.created_at asc
                limit 5
                """,
                params,
            ).fetchall()
            done_today_rows = conn.execute(
                f"""
                select t.id, t.title, t.status, t.due_at, t.completed_at,
                    u.name as assignee_name, c.name as client_name
                from tasks t
                join users u on u.id = t.assigned_to
                left join clients c on c.id = t.client_id
                where t.company_id = %(company_id)s
                  {member_clause}
                  and t.status = 'done'
                  and t.completed_at >= date_trunc('day', now())
                  and t.completed_at < date_trunc('day', now()) + interval '1 day'
                order by t.completed_at desc
                limit 5
                """,
                params,
            ).fetchall()

        summary = {"pending": 0, "done": 0, "overdue": 0}
        for row in rows:
            if row["bucket"] in summary:
                summary[row["bucket"]] = row["total"]
        return summary | {
            "pending_tasks": [_team_task_from_row(row) for row in pending_rows],
            "overdue_tasks": [_team_task_from_row(row) for row in overdue_rows],
            "done_today_tasks": [_team_task_from_row(row) for row in done_today_rows],
        }

    def save_pending_choice(
        self,
        company_id: str,
        user_id: str,
        action: str,
        matches: list[dict[str, Any]],
        params: dict[str, Any] | None = None,
        ttl_minutes: int = 10,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into pending_task_choices (
                    company_id, user_id, action, matches, params, expires_at
                )
                values (
                    %(company_id)s, %(user_id)s, %(action)s, %(matches)s, %(params)s,
                    now() + (%(ttl_minutes)s::text || ' minutes')::interval
                )
                on conflict (company_id, user_id)
                do update set
                    action = excluded.action,
                    matches = excluded.matches,
                    params = excluded.params,
                    expires_at = excluded.expires_at,
                    created_at = now()
                """,
                {
                    "company_id": company_id,
                    "user_id": user_id,
                    "action": action,
                    "matches": Jsonb(matches),
                    "params": Jsonb(params or {}),
                    "ttl_minutes": ttl_minutes,
                },
            )
            conn.commit()

    def get_pending_choice(self, company_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                delete from pending_task_choices
                where expires_at < now()
                """
            )
            row = conn.execute(
                """
                select action, matches, params, expires_at
                from pending_task_choices
                where company_id = %(company_id)s
                  and user_id = %(user_id)s
                limit 1
                """,
                {"company_id": company_id, "user_id": user_id},
            ).fetchone()
            conn.commit()
        return dict(row) if row else None

    def clear_pending_choice(self, company_id: str, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                delete from pending_task_choices
                where company_id = %(company_id)s
                  and user_id = %(user_id)s
                """,
                {"company_id": company_id, "user_id": user_id},
            )
            conn.commit()

    def save_pending_task_draft(
        self,
        company_id: str,
        user_id: str,
        params: dict[str, Any],
        ttl_minutes: int = 30,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into pending_task_drafts (
                    company_id, user_id, params, expires_at
                )
                values (
                    %(company_id)s, %(user_id)s, %(params)s,
                    now() + (%(ttl_minutes)s::text || ' minutes')::interval
                )
                on conflict (company_id, user_id)
                do update set
                    params = excluded.params,
                    expires_at = excluded.expires_at,
                    created_at = now()
                """,
                {
                    "company_id": company_id,
                    "user_id": user_id,
                    "params": Jsonb(params),
                    "ttl_minutes": ttl_minutes,
                },
            )
            conn.commit()

    def get_pending_task_draft(self, company_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("delete from pending_task_drafts where expires_at < now()")
            row = conn.execute(
                """
                select params, expires_at
                from pending_task_drafts
                where company_id = %(company_id)s
                  and user_id = %(user_id)s
                limit 1
                """,
                {"company_id": company_id, "user_id": user_id},
            ).fetchone()
            conn.commit()
        return dict(row) if row else None

    def clear_pending_task_draft(self, company_id: str, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                delete from pending_task_drafts
                where company_id = %(company_id)s
                  and user_id = %(user_id)s
                """,
                {"company_id": company_id, "user_id": user_id},
            )
            conn.commit()


    def save_pending_invite_draft(
        self,
        company_id: str,
        user_id: str,
        params: dict[str, Any],
        ttl_minutes: int = 24 * 60,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into pending_invite_drafts (
                    company_id, user_id, params, expires_at
                )
                values (
                    %(company_id)s, %(user_id)s, %(params)s,
                    now() + (%(ttl_minutes)s::text || ' minutes')::interval
                )
                on conflict (company_id, user_id)
                do update set
                    params = excluded.params,
                    expires_at = excluded.expires_at,
                    created_at = now()
                """,
                {
                    "company_id": company_id,
                    "user_id": user_id,
                    "params": Jsonb(params),
                    "ttl_minutes": ttl_minutes,
                },
            )
            conn.commit()

    def get_pending_invite_draft(self, company_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("delete from pending_invite_drafts where expires_at < now()")
            row = conn.execute(
                """
                select params, expires_at
                from pending_invite_drafts
                where company_id = %(company_id)s
                  and user_id = %(user_id)s
                limit 1
                """,
                {"company_id": company_id, "user_id": user_id},
            ).fetchone()
            conn.commit()
        return dict(row) if row else None

    def clear_pending_invite_draft(self, company_id: str, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                delete from pending_invite_drafts
                where company_id = %(company_id)s
                  and user_id = %(user_id)s
                """,
                {"company_id": company_id, "user_id": user_id},
            )
            conn.commit()
    def get_onboarding_session(self, phone: str) -> dict[str, Any] | None:
        phones = phone_variants(phone)
        with self._connect() as conn:
            row = conn.execute(
                """
                select phone, step, company_name, user_name, job_title
                from onboarding_sessions
                where phone = any(%(phones)s)
                limit 1
                """,
                {"phones": phones},
            ).fetchone()
        return dict(row) if row else None

    def start_onboarding_session(self, phone: str) -> dict[str, Any]:
        phone = normalize_phone(phone)
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into onboarding_sessions (phone, step)
                values (%(phone)s, 'company_name')
                on conflict (phone)
                do update set updated_at = now()
                returning phone, step, company_name, user_name, job_title
                """,
                {"phone": phone},
            ).fetchone()
            conn.commit()
        return dict(row)

    def update_onboarding_session(self, phone: str, **updates) -> dict[str, Any]:
        phone = normalize_phone(phone)
        allowed_fields = {"step", "company_name", "user_name", "job_title"}
        fields = {key: value for key, value in updates.items() if key in allowed_fields}
        if not fields:
            session = self.get_onboarding_session(phone)
            if session is None:
                raise ValueError("onboarding_session_not_found")
            return session

        assignments = ", ".join(f"{field} = %({field})s" for field in fields)
        params = fields | {"phone": phone}
        with self._connect() as conn:
            row = conn.execute(
                f"""
                update onboarding_sessions
                set {assignments}, updated_at = now()
                where phone = %(phone)s
                returning phone, step, company_name, user_name, job_title
                """,
                params,
            ).fetchone()
            conn.commit()
        if row is None:
            raise ValueError("onboarding_session_not_found")
        return dict(row)

    def clear_onboarding_session(self, phone: str) -> None:
        phones = phone_variants(phone)
        with self._connect() as conn:
            conn.execute(
                "delete from onboarding_sessions where phone = any(%(phones)s)",
                {"phones": phones},
            )
            conn.commit()

    def complete_onboarding_session(self, phone: str) -> dict[str, str]:
        phone = normalize_phone(phone)
        with self._connect() as conn:
            session = conn.execute(
                """
                select phone, company_name, user_name, job_title
                from onboarding_sessions
                where phone = %(phone)s
                limit 1
                """,
                {"phone": phone},
            ).fetchone()
            if session is None:
                raise ValueError("onboarding_session_not_found")

            company = conn.execute(
                "insert into companies (name) values (%(name)s) returning id, name",
                {"name": session["company_name"]},
            ).fetchone()
            user = conn.execute(
                """
                insert into users (name, phone, job_title)
                values (%(name)s, %(phone)s, %(job_title)s)
                returning id, name, job_title
                """,
                {
                    "name": session["user_name"],
                    "phone": phone,
                    "job_title": session["job_title"],
                },
            ).fetchone()
            conn.execute(
                """
                insert into company_members (company_id, user_id, role)
                values (%(company_id)s, %(user_id)s, 'owner')
                """,
                {"company_id": company["id"], "user_id": user["id"]},
            )
            conn.execute(
                "delete from onboarding_sessions where phone = %(phone)s",
                {"phone": phone},
            )
            conn.commit()

        return {
            "company_id": str(company["id"]),
            "company_name": company["name"],
            "user_id": str(user["id"]),
            "user_name": user["name"],
            "job_title": user["job_title"],
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
    ) -> dict[str, Any]:
        phone = normalize_phone(phone)
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into invites (
                    company_id, invited_by, phone, name, job_title, role, expires_at
                )
                values (
                    %(company_id)s, %(invited_by)s, %(phone)s, %(name)s,
                    %(job_title)s, %(role)s, %(expires_at)s
                )
                on conflict (company_id, phone)
                where status = 'pending'
                do update set
                    invited_by = excluded.invited_by,
                    name = excluded.name,
                    job_title = excluded.job_title,
                    role = excluded.role,
                    expires_at = excluded.expires_at
                returning id, company_id, invited_by, phone, name, job_title, role,
                    status, accepted_at, declined_at, expires_at
                """,
                {
                    "company_id": company_id,
                    "invited_by": invited_by,
                    "phone": phone,
                    "name": name,
                    "job_title": job_title,
                    "role": role,
                    "expires_at": expires_at,
                },
            ).fetchone()
            conn.commit()
        return _invite_from_row(row)

    def get_pending_invite_by_phone(self, phone: str) -> dict[str, Any] | None:
        phones = phone_variants(phone)
        with self._connect() as conn:
            row = conn.execute(
                """
                select
                    i.id, i.company_id, i.invited_by, i.phone, i.name, i.job_title, i.role,
                    i.status, i.accepted_at, i.declined_at, i.expires_at,
                    c.name as company_name,
                    inviter.name as invited_by_name
                from invites i
                join companies c on c.id = i.company_id
                left join users inviter on inviter.id = i.invited_by
                where i.phone = any(%(phones)s)
                  and i.status = 'pending'
                  and (i.expires_at is null or i.expires_at > now())
                order by i.created_at desc
                limit 1
                """,
                {"phones": phones},
            ).fetchone()
        return _invite_from_row(row) if row else None

    def accept_invite(self, invite_id: str, phone: str) -> dict[str, Any] | None:
        phone = normalize_phone(phone)
        phones = phone_variants(phone)
        with self._connect() as conn:
            invite = conn.execute(
                """
                select
                    i.id, i.company_id, i.phone, i.name, i.job_title, i.role,
                    c.name as company_name
                from invites i
                join companies c on c.id = i.company_id
                where i.id = %(invite_id)s
                  and i.phone = any(%(phones)s)
                  and i.status = 'pending'
                  and (i.expires_at is null or i.expires_at > now())
                limit 1
                """,
                {"invite_id": invite_id, "phones": phones},
            ).fetchone()

            if invite is None:
                return None

            user = conn.execute(
                """
                insert into users (name, phone, job_title)
                values (%(name)s, %(phone)s, %(job_title)s)
                on conflict (phone)
                do update set
                    name = coalesce(excluded.name, users.name),
                    job_title = coalesce(excluded.job_title, users.job_title)
                returning id, name, job_title
                """,
                {
                    "name": invite["name"] or "Usuario",
                    "phone": phone,
                    "job_title": invite["job_title"],
                },
            ).fetchone()

            conn.execute(
                """
                insert into company_members (company_id, user_id, role)
                values (%(company_id)s, %(user_id)s, %(role)s)
                on conflict (company_id, user_id)
                do update set role = excluded.role
                """,
                {
                    "company_id": invite["company_id"],
                    "user_id": user["id"],
                    "role": invite["role"],
                },
            )
            conn.execute(
                """
                update invites
                set status = 'accepted',
                    accepted_at = now()
                where id = %(invite_id)s
                """,
                {"invite_id": invite_id},
            )
            conn.execute(
                "delete from onboarding_sessions where phone = any(%(phones)s)",
                {"phones": phones},
            )
            conn.commit()

        return {
            "company_id": str(invite["company_id"]),
            "company_name": invite["company_name"],
            "user_id": str(user["id"]),
            "user_name": user["name"],
            "job_title": user["job_title"],
            "role": invite["role"],
        }

    def decline_invite(self, invite_id: str) -> dict[str, Any] | None:
        return self.mark_invite_status(invite_id, InviteStatus.declined.value, datetime.now())

    def mark_invite_status(self, invite_id: str, status: str, timestamp: datetime) -> dict[str, Any] | None:
        accepted_at = timestamp if status == "accepted" else None
        declined_at = timestamp if status == "declined" else None
        with self._connect() as conn:
            row = conn.execute(
                """
                update invites
                set status = %(status)s,
                    accepted_at = coalesce(%(accepted_at)s, accepted_at),
                    declined_at = coalesce(%(declined_at)s, declined_at)
                where id = %(invite_id)s
                returning id, company_id, invited_by, phone, name, job_title, role,
                    status, accepted_at, declined_at, expires_at
                """,
                {
                    "invite_id": invite_id,
                    "status": status,
                    "accepted_at": accepted_at,
                    "declined_at": declined_at,
                },
            ).fetchone()
            conn.commit()
        return _invite_from_row(row) if row else None

    def enqueue_notification(
        self,
        company_id: str,
        task_id: str | None,
        recipient_user_id: str | None,
        recipient_phone: str,
        notification_type: str,
        message: str,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into notification_outbox (
                    company_id, task_id, recipient_user_id, recipient_phone,
                    notification_type, message
                )
                values (
                    %(company_id)s, %(task_id)s, %(recipient_user_id)s, %(recipient_phone)s,
                    %(notification_type)s, %(message)s
                )
                returning id, company_id, task_id, recipient_user_id, recipient_phone,
                    notification_type, message, status, attempts, last_error,
                    next_attempt_at, sent_at
                """,
                {
                    "company_id": company_id,
                    "task_id": task_id,
                    "recipient_user_id": recipient_user_id,
                    "recipient_phone": recipient_phone,
                    "notification_type": notification_type,
                    "message": message,
                },
            ).fetchone()
            conn.commit()
        return _notification_from_row(row)

    def list_due_notifications(self, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, company_id, task_id, recipient_user_id, recipient_phone,
                    notification_type, message, status, attempts, last_error,
                    next_attempt_at, sent_at
                from notification_outbox
                where status = 'pending'
                  and next_attempt_at <= %(now)s
                order by next_attempt_at asc, created_at asc
                limit %(limit)s
                """,
                {"now": now, "limit": limit},
            ).fetchall()
        return [_notification_from_row(row) for row in rows]

    def mark_notification_sent(
        self,
        notification_id: str,
        sent_at: datetime,
        provider_response: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update notification_outbox
                set status = 'sent',
                    sent_at = %(sent_at)s,
                    provider_response = %(provider_response)s,
                    updated_at = now()
                where id = %(notification_id)s
                """,
                {
                    "notification_id": notification_id,
                    "sent_at": sent_at,
                    "provider_response": Jsonb(provider_response or {}),
                },
            )
            conn.commit()

    def mark_notification_failed(
        self,
        notification_id: str,
        error: str,
        failed_at: datetime,
        max_attempts: int = 5,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                update notification_outbox
                set attempts = attempts + 1,
                    last_error = %(error)s,
                    status = case when attempts + 1 >= %(max_attempts)s then 'failed' else 'pending' end,
                    next_attempt_at = case
                        when attempts + 1 >= %(max_attempts)s then %(failed_at)s
                        else %(failed_at)s + (
                            least(30, power(2, attempts + 1))::text || ' minutes'
                        )::interval
                    end,
                    updated_at = now()
                where id = %(notification_id)s
                returning status
                """,
                {
                    "notification_id": notification_id,
                    "error": error,
                    "failed_at": failed_at,
                    "max_attempts": max_attempts,
                },
            ).fetchone()
            conn.commit()
        return bool(row and row["status"] == "failed")

    def count_failed_notifications(self, since: datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                select count(*) as total
                from notification_outbox
                where status = 'failed'
                  and updated_at >= %(since)s
                """,
                {"since": since},
            ).fetchone()
        return int(row["total"]) if row else 0

    def log_whatsapp_message(
        self,
        company_id: str,
        user_id: str,
        provider_message_id: str | None,
        body: str,
        parsed: dict[str, Any],
        response_body: str,
        error: str | None = None,
        from_phone: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into whatsapp_messages (
                    company_id, user_id, provider_message_id, direction, body,
                    parsed, response_body, error
                )
                values (
                    %(company_id)s, %(user_id)s, %(provider_message_id)s, 'inbound',
                    %(body)s, %(parsed)s, %(response_body)s, %(error)s
                )
                """,
                {
                    "company_id": company_id,
                    "user_id": user_id,
                    "provider_message_id": provider_message_id,
                    "body": body,
                    "parsed": Jsonb(parsed),
                    "response_body": response_body,
                    "error": error,
                },
            )
            conn.commit()

    def get_message_by_provider_id(self, provider_message_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select response_body
                from whatsapp_messages
                where provider_message_id = %(provider_message_id)s
                limit 1
                """,
                {"provider_message_id": provider_message_id},
            ).fetchone()
        return row["response_body"] if row else None

    def get_recent_messages(self, company_id: str, user_id: str, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select direction, body, response_body, created_at
                from whatsapp_messages
                where company_id = %(company_id)s and user_id = %(user_id)s
                order by created_at desc
                limit %(limit)s
                """,
                {"company_id": company_id, "user_id": user_id, "limit": limit},
            ).fetchall()
        history = []
        for row in reversed(rows):
            history.append({"direction": "inbound", "body": row["body"]})
            if row["response_body"]:
                history.append({"direction": "outbound", "body": row["response_body"]})
        return history

    def get_observe_messages(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select company_id::text, user_id::text, provider_message_id,
                       body, parsed, response_body, error, created_at
                from whatsapp_messages
                order by created_at desc
                limit %(limit)s
                """,
                {"limit": limit},
            ).fetchall()
        return [
            {
                "ts": row["created_at"].isoformat() if row["created_at"] else "",
                "company_id": row["company_id"],
                "user_id": row["user_id"],
                "from_phone": "",
                "provider_message_id": row["provider_message_id"],
                "body": row["body"] or "",
                "parsed": row["parsed"] or {},
                "response_body": row["response_body"] or "",
                "error": row["error"],
            }
            for row in rows
        ]

    def list_reminder_candidates(
        self,
        now: datetime,
        window_minutes: int = 60,
    ) -> list[ReminderCandidate]:
        from whatsapp_task_agent.settings import settings as _settings
        overdue_max_age_days = _settings.overdue_reminder_max_age_days
        with self._connect() as conn:
            rows = conn.execute(
                """
                with candidates as (
                    select
                        t.id as task_id,
                        t.company_id,
                        t.title,
                        t.assigned_to,
                        u.name as assignee_name,
                        u.phone as assignee_phone,
                        t.due_at,
                        case
                            when t.due_at < %(now)s then 'overdue'
                            when t.due_at <= %(now)s + interval '5 minutes' then 'due_now'
                            else 'due_soon'
                        end as reminder_kind
                    from tasks t
                    join users u on u.id = t.assigned_to
                    where t.status in ('pending', 'in_progress', 'overdue')
                      and t.due_at is not null
                      and t.due_at <= %(now)s + (%(window_minutes)s::text || ' minutes')::interval
                      and t.due_at >= %(now)s - (%(max_age_days)s::text || ' days')::interval
                )
                select *
                from candidates c
                where not exists (
                    select 1
                    from task_reminders tr
                    where tr.task_id = c.task_id
                      and tr.reminder_kind = c.reminder_kind
                      and tr.sent_at is not null
                )
                order by c.due_at asc
                limit 100
                """,
                {"now": now, "window_minutes": window_minutes, "max_age_days": overdue_max_age_days},
            ).fetchall()

        return [
            ReminderCandidate(
                task_id=row["task_id"],
                company_id=row["company_id"],
                title=row["title"],
                assigned_to=row["assigned_to"],
                assignee_name=row["assignee_name"],
                assignee_phone=row["assignee_phone"],
                due_at=row["due_at"],
                kind=ReminderKind(row["reminder_kind"]),
            )
            for row in rows
        ]

    def claim_reminder_candidates(self, candidates: list[ReminderCandidate]) -> list[ReminderCandidate]:
        """Atomically claim candidates before sending. Returns only the ones this job owns.

        Uses INSERT ... ON CONFLICT to guarantee at-most-one sender per (task, kind).
        Stale claims (sent_at IS NULL and claimed > 5 min ago) are re-claimable so a
        crashed job never permanently blocks a reminder.
        """
        if not candidates:
            return []
        claimed = []
        with self._connect() as conn:
            for candidate in candidates:
                row = conn.execute(
                    """
                    insert into task_reminders (task_id, reminder_kind, remind_at)
                    values (%(task_id)s, %(reminder_kind)s, now())
                    on conflict (task_id, reminder_kind) do update
                        set remind_at = now()
                        where task_reminders.sent_at is null
                          and task_reminders.remind_at < now() - interval '5 minutes'
                    returning task_id
                    """,
                    {
                        "task_id": candidate.task_id,
                        "reminder_kind": candidate.kind.value,
                    },
                ).fetchone()
                if row is not None:
                    claimed.append(candidate)
            conn.commit()
        return claimed

    def mark_reminder_sent(self, candidate: ReminderCandidate, sent_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update task_reminders
                set sent_at = %(sent_at)s
                where task_id = %(task_id)s
                  and reminder_kind = %(reminder_kind)s
                """,
                {
                    "task_id": candidate.task_id,
                    "reminder_kind": candidate.kind.value,
                    "sent_at": sent_at,
                },
            )
            conn.execute(
                """
                insert into task_events (task_id, actor_id, event_type, payload)
                values (%(task_id)s, %(actor_id)s, 'reminder_sent', %(payload)s)
                """,
                {
                    "task_id": candidate.task_id,
                    "actor_id": candidate.assigned_to,
                    "payload": Jsonb({"kind": candidate.kind.value, "source": "reminder_graph"}),
                },
            )
            conn.commit()

    def _find_task_candidates(
        self,
        company_id: str,
        user_id: str,
        task_reference: str,
    ) -> list[Task]:
        params: dict[str, Any] = {
            "company_id": company_id,
            "user_id": user_id,
            "reference": f"%{task_reference}%",
            "id_prefix": f"{task_reference}%",
        }
        with self._connect() as conn:
            matches = conn.execute(
                """
                select t.id, t.company_id, t.client_id, c.name as client_name, t.title,
                    t.assigned_to, t.created_by, t.status, t.priority, t.due_at,
                    t.completed_at, t.description
                from tasks t
                left join clients c on c.id = t.client_id
                where t.company_id = %(company_id)s
                  and (t.assigned_to = %(user_id)s or t.created_by = %(user_id)s)
                  and t.status in ('pending', 'in_progress', 'overdue')
                  and (t.title ilike %(reference)s or t.id::text like %(id_prefix)s)
                order by t.due_at asc nulls last, t.created_at asc
                limit 6
                """,
                params,
            ).fetchall()

            if not matches:
                candidates = conn.execute(
                    """
                    select t.id, t.company_id, t.client_id, c.name as client_name, t.title,
                        t.assigned_to, t.created_by, t.status, t.priority, t.due_at,
                        t.completed_at, t.description
                    from tasks t
                    left join clients c on c.id = t.client_id
                    where t.company_id = %(company_id)s
                      and (t.assigned_to = %(user_id)s or t.created_by = %(user_id)s)
                      and t.status in ('pending', 'in_progress', 'overdue')
                    order by t.due_at asc nulls last, t.created_at asc
                    limit 50
                    """,
                    {"company_id": company_id, "user_id": user_id},
                ).fetchall()
                scored = sorted(
                    ((row, _fuzzy_match_score(task_reference, row["title"])) for row in candidates),
                    key=lambda x: -x[1],
                )
                matches = [row for row, score in scored if score >= 0.65][:5]

        return [_task_from_row(row) for row in matches]

    def _update_referenced_task(
        self,
        company_id: str,
        user_id: str,
        task_reference: str,
        updates: str,
        event_type: str,
        extra_params: dict[str, Any] | None = None,
    ) -> Task | list[Task] | None:
        params: dict[str, Any] = {
            "company_id": company_id,
            "user_id": user_id,
            "reference": f"%{task_reference}%",
            "id_prefix": f"{task_reference}%",
        }
        params.update(extra_params or {})
        with self._connect() as conn:
            matches = conn.execute(
                """
                select t.id, t.company_id, t.client_id, c.name as client_name, t.title,
                    t.assigned_to, t.created_by, t.status, t.priority, t.due_at,
                    t.completed_at, t.description
                from tasks t
                left join clients c on c.id = t.client_id
                where t.company_id = %(company_id)s
                  and (t.assigned_to = %(user_id)s or t.created_by = %(user_id)s)
                  and t.status in ('pending', 'in_progress', 'overdue')
                  and (t.title ilike %(reference)s or t.id::text like %(id_prefix)s)
                order by t.due_at asc nulls last, t.created_at asc
                limit 6
                """,
                params,
            ).fetchall()

            if not matches:
                candidates = conn.execute(
                    """
                    select t.id, t.company_id, t.client_id, c.name as client_name, t.title,
                        t.assigned_to, t.created_by, t.status, t.priority, t.due_at,
                        t.completed_at, t.description
                    from tasks t
                    left join clients c on c.id = t.client_id
                    where t.company_id = %(company_id)s
                      and (t.assigned_to = %(user_id)s or t.created_by = %(user_id)s)
                      and t.status in ('pending', 'in_progress', 'overdue')
                    order by t.due_at asc nulls last, t.created_at asc
                    limit 50
                    """,
                    {"company_id": params["company_id"], "user_id": params["user_id"]},
                ).fetchall()
                scored = sorted(
                    ((row, _fuzzy_match_score(task_reference, row["title"])) for row in candidates),
                    key=lambda x: -x[1],
                )
                matches = [row for row, score in scored if score >= 0.65][:5]

            if not matches:
                return None

            if len(matches) > 1:
                return [_task_from_row(match) for match in matches[:5]]

            row = conn.execute(
                f"""
                update tasks
                set {updates}
                where id = %(task_id)s
                returning id, company_id, client_id, title, assigned_to, created_by, status,
                    priority, due_at, completed_at, description
                """,
                params | {"task_id": matches[0]["id"]},
            ).fetchone()

            if row is None:
                return None

            conn.execute(
                """
                insert into task_events (task_id, actor_id, event_type, payload)
                values (%(task_id)s, %(actor_id)s, %(event_type)s, %(payload)s)
                """,
                {
                    "task_id": row["id"],
                    "actor_id": user_id,
                    "event_type": event_type,
                    "payload": Jsonb({"source": "whatsapp"}),
                },
            )
            conn.commit()

        return self._hydrate_task_client(_task_from_row(row))

    def _update_task_by_id(
        self,
        company_id: str,
        user_id: str,
        task_id: str,
        updates: str,
        event_type: str,
        extra_params: dict[str, Any] | None = None,
    ) -> Task | None:
        params: dict[str, Any] = {
            "company_id": company_id,
            "user_id": user_id,
            "task_id": task_id,
        }
        params.update(extra_params or {})
        with self._connect() as conn:
            row = conn.execute(
                f"""
                update tasks
                set {updates}
                where id = %(task_id)s
                  and company_id = %(company_id)s
                  and (assigned_to = %(user_id)s or created_by = %(user_id)s)
                  and status in ('pending', 'in_progress', 'overdue')
                returning id, company_id, client_id, title, assigned_to, created_by, status,
                    priority, due_at, completed_at, description
                """,
                params,
            ).fetchone()

            if row is None:
                return None

            conn.execute(
                """
                insert into task_events (task_id, actor_id, event_type, payload)
                values (%(task_id)s, %(actor_id)s, %(event_type)s, %(payload)s)
                """,
                {
                    "task_id": row["id"],
                    "actor_id": user_id,
                    "event_type": event_type,
                    "payload": Jsonb({"source": "whatsapp_pending_choice"}),
                },
            )
            conn.commit()

        return self._hydrate_task_client(_task_from_row(row))

    def _hydrate_task_client(self, task: Task) -> Task:
        if not task.client_id:
            return task
        with self._connect() as conn:
            row = conn.execute("select name from clients where id = %(client_id)s", {"client_id": task.client_id}).fetchone()
        if row:
            task.client_name = row["name"]
        return task

    def _connect(self):
        if self._pool.closed:
            self._pool.open()
        return self._pool.connection()


def _task_from_row(row: dict[str, Any]) -> Task:
    return Task(
        id=row["id"],
        company_id=row["company_id"],
        client_id=row.get("client_id"),
        client_name=row.get("client_name"),
        title=row["title"],
        assigned_to=row["assigned_to"],
        created_by=row["created_by"],
        status=TaskStatus(row["status"]),
        priority=Priority(row["priority"]) if row["priority"] else None,
        due_at=row["due_at"],
        completed_at=row.get("completed_at"),
        description=row["description"],
    )


def _normalize_name(value: str) -> str:
    text = normalize("NFKD", value.strip().lower())
    return "".join(char for char in text if char.isascii())


def _fuzzy_match_score(reference: str, title: str) -> float:
    def _norm(v: str) -> str:
        text = normalize("NFKD", v.strip().lower())
        return "".join(c for c in text if c.isascii())

    ref = _norm(reference)
    ttl = _norm(title)

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


def _team_task_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "assignee_name": row["assignee_name"],
        "client_name": row.get("client_name"),
        "status": row["status"],
        "due_at": row["due_at"].isoformat() if row["due_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
    }


def _notification_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "company_id": str(row["company_id"]),
        "task_id": str(row["task_id"]) if row["task_id"] else None,
        "recipient_user_id": str(row["recipient_user_id"]) if row["recipient_user_id"] else None,
        "recipient_phone": row["recipient_phone"],
        "notification_type": row["notification_type"],
        "message": row["message"],
        "status": row["status"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "next_attempt_at": row["next_attempt_at"],
        "sent_at": row["sent_at"],
    }


def _invite_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "company_id": str(row["company_id"]),
        "invited_by": str(row["invited_by"]) if row["invited_by"] else None,
        "phone": row["phone"],
        "name": row["name"],
        "job_title": row["job_title"],
        "role": row["role"],
        "status": row["status"],
        "accepted_at": row["accepted_at"],
        "declined_at": row["declined_at"],
        "expires_at": row["expires_at"],
        "company_name": row.get("company_name"),
        "invited_by_name": row.get("invited_by_name"),
    }

