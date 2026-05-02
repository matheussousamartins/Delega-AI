create extension if not exists "pgcrypto";

do $$ begin
    create type app_role as enum ('owner', 'admin', 'manager', 'member');
exception when duplicate_object then null;
end $$;

do $$ begin
    create type task_status as enum ('pending', 'in_progress', 'done', 'cancelled', 'overdue');
exception when duplicate_object then null;
end $$;

do $$ begin
    create type task_priority as enum ('low', 'medium', 'high');
exception when duplicate_object then null;
end $$;

do $$ begin
    create type message_direction as enum ('inbound', 'outbound');
exception when duplicate_object then null;
end $$;

do $$ begin
    create type invite_status as enum ('pending', 'accepted', 'declined', 'expired', 'cancelled');
exception when duplicate_object then null;
end $$;

create table if not exists companies (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    created_at timestamptz not null default now()
);

create table if not exists users (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    email text unique,
    phone text unique,
    job_title text,
    created_at timestamptz not null default now()
);

create table if not exists company_members (
    company_id uuid not null references companies(id) on delete cascade,
    user_id uuid not null references users(id) on delete cascade,
    role app_role not null default 'member',
    created_at timestamptz not null default now(),
    primary key (company_id, user_id)
);

create index if not exists idx_company_members_user_id
    on company_members (user_id);

create table if not exists clients (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    name text not null,
    normalized_name text not null,
    created_at timestamptz not null default now(),
    constraint uq_clients_company_normalized_name unique (company_id, normalized_name)
);

create table if not exists invites (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    invited_by uuid references users(id),
    name text,
    phone text not null,
    job_title text,
    role app_role not null default 'member',
    status invite_status not null default 'pending',
    accepted_at timestamptz,
    declined_at timestamptz,
    expires_at timestamptz,
    created_at timestamptz not null default now()
);

create unique index if not exists uq_invites_pending_company_phone
    on invites (company_id, phone)
    where status = 'pending';

create index if not exists idx_invites_phone_status
    on invites (phone, status);

create table if not exists tasks (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    client_id uuid references clients(id) on delete set null,
    title text not null,
    description text,
    status task_status not null default 'pending',
    priority task_priority,
    assigned_to uuid references users(id),
    created_by uuid references users(id),
    due_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_tasks_company_assignee_status
    on tasks (company_id, assigned_to, status, due_at);

create index if not exists idx_tasks_company_client
    on tasks (company_id, client_id);

create table if not exists task_comments (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references tasks(id) on delete cascade,
    user_id uuid references users(id),
    body text not null,
    created_at timestamptz not null default now()
);

create table if not exists task_events (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references tasks(id) on delete cascade,
    actor_id uuid references users(id),
    event_type text not null,
    payload jsonb not null default '{}',
    created_at timestamptz not null default now()
);

create table if not exists whatsapp_messages (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    user_id uuid references users(id),
    provider_message_id text,
    direction message_direction not null,
    body text not null,
    parsed jsonb,
    response_body text,
    error text,
    created_at timestamptz not null default now()
);

create unique index if not exists idx_whatsapp_messages_provider_id
    on whatsapp_messages (provider_message_id)
    where provider_message_id is not null;

create table if not exists pending_task_choices (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    user_id uuid not null references users(id) on delete cascade,
    action text not null check (action in (
        'complete_task',
        'reschedule_task',
        'select_task_for_reschedule',
        'start_task',
        'confirm_create_task_assignee',
        'paginate_tasks',
        'show_task_details',
        'cannot_do_task',
        'needs_help_task',
        'reassign_task'
    )),
    matches jsonb not null,
    params jsonb not null default '{}',
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    constraint uq_pending_task_choices_user unique (company_id, user_id)
);

create index if not exists idx_pending_task_choices_expires_at
    on pending_task_choices (expires_at);

create table if not exists pending_task_drafts (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    user_id uuid not null references users(id) on delete cascade,
    params jsonb not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    constraint uq_pending_task_drafts_user unique (company_id, user_id)
);

create index if not exists idx_pending_task_drafts_expires_at
    on pending_task_drafts (expires_at);

create table if not exists pending_invite_drafts (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    user_id uuid not null references users(id) on delete cascade,
    params jsonb not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    constraint uq_pending_invite_drafts_user unique (company_id, user_id)
);

create index if not exists idx_pending_invite_drafts_expires_at
    on pending_invite_drafts (expires_at);

create table if not exists task_reminders (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references tasks(id) on delete cascade,
    reminder_kind text not null default 'due_soon',
    remind_at timestamptz not null,
    sent_at timestamptz,
    created_at timestamptz not null default now(),
    constraint uq_task_reminders_task_kind unique (task_id, reminder_kind)
);

create index if not exists idx_task_reminders_stale_claims
    on task_reminders (remind_at)
    where sent_at is null;

create table if not exists notification_outbox (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    task_id uuid references tasks(id) on delete cascade,
    recipient_user_id uuid references users(id) on delete cascade,
    recipient_phone text not null,
    notification_type text not null,
    message text not null,
    status text not null default 'pending' check (status in ('pending', 'sent', 'failed')),
    attempts int not null default 0,
    last_error text,
    provider_response jsonb,
    next_attempt_at timestamptz not null default now(),
    sent_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_notification_outbox_due
    on notification_outbox (status, next_attempt_at);

create index if not exists idx_notification_outbox_failed
    on notification_outbox (status, updated_at)
    where status = 'failed';

create table if not exists onboarding_sessions (
    phone text primary key,
    step text not null,
    company_name text,
    user_name text,
    job_title text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists leads (
    id uuid primary key default gen_random_uuid(),
    company_id uuid references companies(id) on delete cascade,
    name varchar(150) not null,
    phone varchar(30) not null,
    source varchar(50) not null default 'whatsapp',
    created_at timestamptz not null default now(),
    constraint uq_leads_company_phone unique (company_id, phone),
    constraint chk_leads_phone_format check (phone ~ '^\+?[0-9]{10,15}$')
);

create index if not exists idx_leads_created_at
    on leads (created_at desc);

create index if not exists idx_leads_company_phone
    on leads (company_id, phone);
