alter table users
    add column if not exists job_title text;

create table if not exists onboarding_sessions (
    phone text primary key,
    step text not null,
    company_name text,
    user_name text,
    job_title text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
