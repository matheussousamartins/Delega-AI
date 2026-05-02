create table if not exists notification_outbox (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    task_id uuid references tasks(id) on delete cascade,
    recipient_user_id uuid not null references users(id) on delete cascade,
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
