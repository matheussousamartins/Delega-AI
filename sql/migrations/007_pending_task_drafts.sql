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
