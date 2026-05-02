create table if not exists clients (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references companies(id) on delete cascade,
    name text not null,
    normalized_name text not null,
    created_at timestamptz not null default now(),
    constraint uq_clients_company_normalized_name unique (company_id, normalized_name)
);

alter table tasks
    add column if not exists client_id uuid references clients(id) on delete set null;

create index if not exists idx_tasks_company_client
    on tasks (company_id, client_id);
