do $$ begin
    create type invite_status as enum ('pending', 'accepted', 'declined', 'expired', 'cancelled');
exception when duplicate_object then null;
end $$;

alter table invites
    add column if not exists invited_by uuid references users(id),
    add column if not exists name text,
    add column if not exists job_title text,
    add column if not exists status invite_status not null default 'pending',
    add column if not exists declined_at timestamptz,
    add column if not exists expires_at timestamptz;

create unique index if not exists uq_invites_pending_company_phone
    on invites (company_id, phone)
    where status = 'pending';

create index if not exists idx_invites_phone_status
    on invites (phone, status);
