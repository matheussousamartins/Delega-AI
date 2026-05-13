from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, Field


class Intent(StrEnum):
    task = "task"
    query = "query"
    command = "command"
    other = "other"


class Action(StrEnum):
    create_task = "create_task"
    list_my_tasks = "list_my_tasks"
    complete_task = "complete_task"
    start_task = "start_task"
    reschedule_task = "reschedule_task"
    edit_task = "edit_task"
    cancel_task = "cancel_task"
    task_status = "task_status"
    team_summary = "team_summary"
    invite_user = "invite_user"
    edit_member = "edit_member"
    edit_client = "edit_client"
    cannot_do_task = "cannot_do_task"
    needs_help_task = "needs_help_task"
    reassign_task = "reassign_task"
    task_details = "task_details"


class Priority(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class TaskStatus(StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"
    cancelled = "cancelled"
    overdue = "overdue"


class ReminderKind(StrEnum):
    due_soon = "due_soon"
    due_now = "due_now"
    overdue = "overdue"


class InviteStatus(StrEnum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"
    cancelled = "cancelled"


class ParsedCommand(BaseModel):
    intent: Intent = Intent.other
    action: Action | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=0, ge=0, le=100)


class WhatsAppInput(BaseModel):
    from_phone: str
    message: str
    provider_message_id: str | None = None
    quoted_message_body: str | None = None


class AgentResponse(BaseModel):
    reply: str
    parsed: ParsedCommand
    result: dict[str, Any] = Field(default_factory=dict)


class EvolutionWebhookResponse(BaseModel):
    status: str
    reply: str
    normalized: dict[str, Any] = Field(default_factory=dict)
    parsed: ParsedCommand | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    outbound_status: str | None = None
    outbound_error: str | None = None


class ObserveEntry(BaseModel):
    ts: str
    phone_tail: str
    intent: str | None
    action: str | None
    confidence: int | None
    message: str
    reply: str
    error: str | None


class ObserveResponse(BaseModel):
    count: int
    messages: list[ObserveEntry]


class Task(BaseModel):
    id: UUID
    company_id: UUID
    title: str
    assigned_to: UUID
    created_by: UUID
    client_id: UUID | None = None
    client_name: str | None = None
    status: TaskStatus = TaskStatus.pending
    priority: Priority | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    description: str | None = None


class ReminderCandidate(BaseModel):
    task_id: UUID
    company_id: UUID
    title: str
    assigned_to: UUID
    assignee_name: str
    assignee_phone: str
    due_at: datetime
    kind: ReminderKind


class OutboundReminder(BaseModel):
    task_id: UUID
    kind: ReminderKind
    to_phone: str
    message: str


class CompanyContext(TypedDict):
    company_id: str
    company_name: str
    user_id: str
    user_name: str
    role: Literal["owner", "admin", "manager", "member"]


class AgentState(TypedDict, total=False):
    from_phone: str
    message: str
    provider_message_id: str | None
    quoted_message_body: str | None
    context: CompanyContext
    onboarding: dict[str, Any]
    invite: dict[str, Any]
    contact_share: dict[str, Any]
    parsed: ParsedCommand
    result: dict[str, Any]
    reply: str
    error: str | None


class ReminderState(TypedDict, total=False):
    now: datetime
    window_minutes: int
    candidates: list[ReminderCandidate]
    claimed: list[ReminderCandidate]
    outbound: list[OutboundReminder]
    result: dict[str, Any]
