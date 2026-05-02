create table if not exists pending_task_choices (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    user_id uuid not null references users(id) on delete cascade,
    action text not null check (action in ('complete_task', 'reschedule_task')),
    matches jsonb not null,
    params jsonb not null default '{}',
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    constraint uq_pending_task_choices_user unique (company_id, user_id)
);

create index if not exists idx_pending_task_choices_expires_at
    on pending_task_choices (expires_at);
